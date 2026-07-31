import os
from multiprocessing import Process

from biz.utils.log import logger


def handle_queue(function: callable, data: any, token: str, url: str, url_slug: str):
    process = Process(target=function, args=(data, token, url, url_slug))
    process.start()


def handle_agent_queue(function: callable, *args, **kwargs):
    """异步执行外部 Agent review；参数必须是可序列化的 webhook 数据。"""
    process = Process(target=function, args=args, kwargs=kwargs)
    process.start()


# Backward-compatible name for integrations that imported the old helper.
handle_opencode_queue = handle_agent_queue
