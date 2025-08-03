import os
import re
import asyncio
import aiofiles
import time
from collections import deque
from aiohttp import web, WSMsgType

# 配置参数
LOG_FILE = '/var/log/nginx/access-web.log'
WS_HOST = '127.0.0.1'
WS_PORT = 10086
MAX_CACHE_LINES = 10000  # 内存中缓存的最大行数
MAX_HISTORY_LINES = 50000  # 支持的最大历史行数
MAX_CHUNK_SIZE = 10 * 1024 * 1024  # 10MB
POLL_INTERVAL = 1  # 文件轮询间隔(秒)
HEARTBEAT_INTERVAL = 10  # WebSocket心跳间隔(秒)

# 状态码着色规则
STATUS_COLOR_REGEXES = [
    (re.compile(r'(2\d\d$)'), '<span style="font-weight:bold;color:#16C60C">\\1</span>'),
    (re.compile(r'(3\d\d$)'), '<span style="font-weight:bold;color:#3B78FF">\\1</span>'),
    (re.compile(r'(4\d\d$)'), '<span style="font-weight:bold;color:yellow">\\1</span>'),
    (re.compile(r'(5\d\d$)'), '<span style="font-weight:bold;color:red">\\1</span>')
]

# 时间格式转换正则
TIME_CONVERSION_REGEX = re.compile(
    r'\[(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})([+-]\d{2}:\d{2})\]'
)

# 排除规则
EXCLUDE_RULES = [
    re.compile(r'yourdomain\.com:\d+/(last|\d+-\d+|\d+)'),
    re.compile(r'>127\.0\.0\.1</span>'),
    re.compile(r'>::1</span>')
]

# 全局状态
clients = set()
file_position = 0
buffer = b""
total_filtered_lines = 0
filtered_lines = deque(maxlen=MAX_CACHE_LINES)
lock = asyncio.Lock()

def convert_time_format(line):
    """转换时间格式：从ISO8601格式转换为[YYYY.MM.DD HH:MM:SS]格式"""
    def replace_time(match):
        year, month, day, hour, minute, second, tz = match.groups()
        return f"[{year}.{month}.{day} {hour}:{minute}:{second}]"
    
    return TIME_CONVERSION_REGEX.sub(replace_time, line)

def apply_color(line):
    """应用状态码着色并包裹在<p>标签中"""
    # 首先转换时间格式
    line = convert_time_format(line)
    
    # 应用状态码着色
    for regex, replacement in STATUS_COLOR_REGEXES:
        line = regex.sub(replacement, line)
    
    # 包裹在<p>标签中
    return f'<p>{line}</p>'

def should_exclude(line):
    """检查是否应排除该行"""
    return any(rule.search(line) for rule in EXCLUDE_RULES)

async def tail_log():
    """监控日志文件变化并处理新行"""
    global file_position, buffer, total_filtered_lines
    
    while True:
        try:
            async with aiofiles.open(LOG_FILE, 'rb') as f:
                # 获取当前文件大小
                await f.seek(0, os.SEEK_END)
                current_size = await f.tell()
                
                # 处理文件轮转或截断
                if current_size < file_position:
                    file_position = 0
                    buffer = b""
                    await f.seek(0)
                else:
                    await f.seek(file_position)
                
                # 读取新内容
                new_data = b""
                if current_size > file_position:
                    bytes_to_read = min(current_size - file_position, MAX_CHUNK_SIZE)
                    new_data = await f.read(bytes_to_read)
                    file_position += bytes_to_read
                
                if new_data:
                    # 处理新数据
                    data = buffer + new_data
                    lines = data.split(b'\n')
                    buffer = lines.pop()  # 保存不完整的行
                    
                    new_line_count = 0
                    for line_bytes in lines:
                        try:
                            line = line_bytes.decode('utf-8')
                        except UnicodeDecodeError:
                            continue
                            
                        if should_exclude(line):
                            continue
                            
                        # 更新行计数和存储
                        total_filtered_lines += 1
                        filtered_lines.append(line)
                        new_line_count += 1
                    
                    # 通知WebSocket客户端
                    if new_line_count > 0:
                        async with lock:
                            for ws in clients.copy():
                                if not ws.closed:
                                    try:
                                        await ws.send_str(str(total_filtered_lines))
                                    except:
                                        clients.discard(ws)
        
        except Exception as e:
            print(f"日志监控错误: {e}")
        
        await asyncio.sleep(POLL_INTERVAL)

async def websocket_handler(request):
    """处理WebSocket连接"""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    # 注册客户端并发送当前行数
    async with lock:
        clients.add(ws)
    await ws.send_str(str(total_filtered_lines))
    
    # 心跳任务
    async def heartbeat():
        while not ws.closed:
            try:
                # 每10秒发送一次心跳（当前行数）
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await ws.send_str(str(total_filtered_lines))
            except:
                break
    
    # 启动心跳任务
    heartbeat_task = asyncio.create_task(heartbeat())
    
    # 保持连接
    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        # 清理
        heartbeat_task.cancel()
        async with lock:
            clients.discard(ws)
    
    return ws

async def read_log_lines(start_line, end_line):
    """从日志文件中读取指定范围的行（考虑排除规则）"""
    results = []
    current_line = 0
    chunk_size = 1024 * 1024  # 每次读取1MB
    
    try:
        async with aiofiles.open(LOG_FILE, 'rb') as f:
            # 从文件末尾开始读取
            file_size = await f.seek(0, os.SEEK_END)
            position = max(0, file_size - min(file_size, MAX_HISTORY_LINES * 200))  # 假设平均每行200字符
            
            buffer = b""
            while position >= 0 and len(results) < (end_line - start_line + 1):
                # 读取一块数据
                read_size = min(chunk_size, file_size - position)
                await f.seek(position)
                chunk = await f.read(read_size)
                position -= read_size
                
                # 处理数据块
                data = chunk + buffer
                lines = data.split(b'\n')
                buffer = lines.pop(0)  # 保存不完整的行
                
                # 反向处理行
                for line_bytes in reversed(lines):
                    try:
                        line = line_bytes.decode('utf-8')
                    except UnicodeDecodeError:
                        continue
                    
                    if should_exclude(line):
                        continue
                    
                    current_line += 1
                    if current_line >= start_line and current_line <= end_line:
                        results.append((current_line, line))
                    
                    if current_line > end_line:
                        break
                
                if position < 0:
                    break
            
            # 处理剩余缓冲区
            if buffer and len(results) < (end_line - start_line + 1):
                try:
                    line = buffer.decode('utf-8')
                    if not should_exclude(line):
                        current_line += 1
                        if start_line <= current_line <= end_line:
                            results.append((current_line, line))
                except UnicodeDecodeError:
                    pass
    
    except Exception as e:
        print(f"读取日志文件错误: {e}")
    
    # 按行号排序并返回内容
    results.sort(key=lambda x: x[0])
    return [line for _, line in results]

async def http_handler(request):
    """处理HTTP行数请求"""
    try:
        # 解析请求路径
        path = request.path.strip('/')
        if '-' in path:
            parts = path.split('-')
            if len(parts) != 2:
                return web.Response(status=400, text="无效的范围格式")
            start = int(parts[0])
            end = int(parts[1])
        else:
            start = end = int(path)
        
        # 验证范围
        if start < 1 or end < start or end - start > 1000:
            return web.Response(status=400, text="无效的范围值")
        
        # 计算缓存中的行范围
        cache_start = total_filtered_lines - len(filtered_lines) + 1
        cache_end = total_filtered_lines
        
        # 确定请求行是否在缓存中
        if start >= cache_start and end <= cache_end:
            # 从缓存中提取行
            result_lines = []
            for line_num in range(start, end + 1):
                idx = line_num - cache_start
                if 0 <= idx < len(filtered_lines):
                    result_lines.append(filtered_lines[idx])
        else:
            # 从日志文件中读取请求的行
            result_lines = await read_log_lines(start, end)
            if not result_lines:
                return web.Response(status=404, text="请求的行不在可用范围内")
        
        # 应用着色并包裹在<p>标签中
        colored_lines = [apply_color(line) for line in result_lines]
        
        # 将所有行连接成一个HTML字符串
        html_content = ''.join(colored_lines)
        return web.Response(text=html_content, content_type='text/html')
    
    except ValueError:
        return web.Response(status=400, text="无效的行数格式")

async def main():
    """主函数"""
    # 创建HTTP应用
    app = web.Application()
    app.router.add_route('GET', '/last', websocket_handler)
    app.router.add_get('/{range}', http_handler)
    
    # 启动HTTP服务器
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WS_HOST, WS_PORT)
    await site.start()
    
    # 启动日志监控任务
    asyncio.create_task(tail_log())
    
    # 永久运行
    print(f"服务已启动: http://{WS_HOST}:{WS_PORT}")
    print(f"WebSocket 端点: ws://{WS_HOST}:{WS_PORT}/last")
    await asyncio.Future()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n服务已停止")
