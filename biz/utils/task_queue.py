from multiprocessing import Process

from biz.utils.log import logger


def handle_queue(function: callable, data: any, token: str, url: str, url_slug: str):
    process = Process(target=function, args=(data, token, url, url_slug))
    process.start()


def handle_agent_queue(function: callable, *args, **kwargs):
    """Persist external Agent review work; the separate worker executes it."""
    function(*args, **kwargs)


# Backward-compatible name for integrations that imported the old helper.
handle_opencode_queue = handle_agent_queue
