from workflows import agent, log, phase


async def main(args):
    phase("hello")
    result = await agent("Reply with a short greeting.", label="hello")
    log("completed", greeting=result.text)
    return {"greeting": result.text, "args": args}

