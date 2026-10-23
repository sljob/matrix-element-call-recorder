#!/usr/bin/env python3
import asyncio
import os
import signal
import subprocess

async def relay(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data: break
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()

async def client(reader, writer):
    upstream = None
    try:
        incoming, upstream = await asyncio.wait_for(asyncio.open_connection('call', 8080), 10)
        await asyncio.gather(relay(reader, upstream), relay(incoming, writer))
    except (OSError, asyncio.TimeoutError, ConnectionError):
        writer.close()
        if upstream: upstream.close()

async def main():
    server = await asyncio.start_server(client, '127.0.0.1', 8090)
    child = subprocess.Popen(['/entrypoint.sh'])
    loop = asyncio.get_running_loop()
    def stop():
        if child.poll() is None: child.send_signal(signal.SIGTERM)
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop)
    async with server:
        while child.poll() is None: await asyncio.sleep(.25)
    return child.returncode

if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
