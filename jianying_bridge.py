"""Run with the separately installed jianying-local-mcp Python environment."""
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def call(request):
    server = StdioServerParameters(command=sys.executable, args=['-m', 'jianying_local_mcp', 'serve'],
                                   env=request['environment'])
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            reply = await session.call_tool(request['tool'], request['arguments'])
    if reply.isError:
        return {'error': '\n'.join(item.text for item in reply.content if item.type == 'text')}
    data = reply.structuredContent
    if data is None:
        data = json.loads(reply.content[0].text)
    return {'data': data}


if __name__ == '__main__':
    print(json.dumps(asyncio.run(asyncio.wait_for(call(json.load(sys.stdin)), timeout=900)), ensure_ascii=False))
