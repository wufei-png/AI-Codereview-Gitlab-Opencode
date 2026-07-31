"""
Webhook 路由模块
"""

import json
import os
from urllib.parse import urlparse
from flask import Blueprint, request, jsonify

from biz.platforms.gitlab.webhook_handler import slugify_url
from biz.queue.worker import (
    handle_merge_request_event,
    handle_push_event,
    handle_github_pull_request_event,
    handle_github_push_event,
    handle_gitea_pull_request_event,
    handle_gitea_push_event,
)
from biz.utils.log import logger
from biz.agent.config import is_agent_review_enabled
from biz.agent.service import dispatch_agent_review
from biz.utils.flags import env_flag
from biz.utils.task_queue import handle_agent_queue, handle_queue
from biz.utils.webhook_security import verify_webhook

webhook_bp = Blueprint("webhook", __name__)


def _builtin_review_enabled() -> bool:
    """Platform API tokens are only required by the legacy built-in reviewer."""
    return env_flag(os.environ.get("LLM_REVIEW_ENABLED"), default=True)


@webhook_bp.route("/review/webhook", methods=["POST"])
def handle_webhook():
    """
    处理 Webhook 请求的主路由
    """
    # 获取请求的JSON数据
    if request.is_json:
        if is_agent_review_enabled():
            if request.headers.get("X-Gitea-Event"):
                provider = "gitea"
            elif request.headers.get("X-GitHub-Event"):
                provider = "github"
            else:
                provider = "gitlab"
            if not verify_webhook(provider, request.headers, request.get_data(cache=True)):
                return jsonify({"error": "Invalid webhook signature or secret"}), 401
        data = request.get_json()
        if not data:
            return jsonify({"error": "Invalid JSON"}), 400

        # 判断webhook来源
        webhook_source_github = request.headers.get("X-GitHub-Event")
        webhook_source_gitea = request.headers.get("X-Gitea-Event")

        if webhook_source_gitea:  # Gitea webhook优先处理
            return handle_gitea_webhook(webhook_source_gitea, data)
        elif webhook_source_github:  # GitHub webhook
            return handle_github_webhook(webhook_source_github, data)
        else:  # GitLab webhook
            return handle_gitlab_webhook(data)
    else:
        return jsonify({"message": "Invalid data format"}), 400


def handle_github_webhook(event_type, data):
    """
    处理 GitHub Webhook
    """
    # 获取GitHub配置
    github_token = os.getenv("GITHUB_ACCESS_TOKEN") or request.headers.get(
        "X-GitHub-Token"
    )
    if not github_token and _builtin_review_enabled():
        return jsonify({"message": "Missing GitHub access token"}), 400

    github_url = os.getenv("GITHUB_URL") or "https://github.com"
    github_url_slug = slugify_url(github_url)

    # 打印整个payload数据
    logger.info(f"Received GitHub event: {event_type}")
    logger.info(f"Payload: {json.dumps(data)}")

    if event_type == "pull_request":
        # 异步触发显式配置的外部 Agent backend。
        if is_agent_review_enabled():
            handle_agent_queue(dispatch_agent_review, "github", data)
        
        # 使用handle_queue进行异步处理
        handle_queue(
            handle_github_pull_request_event,
            data,
            github_token,
            github_url,
            github_url_slug,
        )
        # 立马返回响应
        return jsonify(
            {
                "message": f"GitHub request received(event_type={event_type}), will process asynchronously."
            }
        ), 200
    elif event_type == "push":
        # 使用handle_queue进行异步处理
        handle_queue(
            handle_github_push_event, data, github_token, github_url, github_url_slug
        )
        # 立马返回响应
        return jsonify(
            {
                "message": f"GitHub request received(event_type={event_type}), will process asynchronously."
            }
        ), 200
    else:
        error_message = f"Only pull_request and push events are supported for GitHub webhook, but received: {event_type}."
        logger.error(error_message)
        return jsonify(error_message), 400


def handle_gitlab_webhook(data):
    """
    处理 GitLab Webhook
    """
    object_kind = data.get("object_kind")

    # 优先从请求头获取，如果没有，则从环境变量获取，如果没有，则从推送事件中获取
    gitlab_url = os.getenv("GITLAB_URL") or request.headers.get("X-Gitlab-Instance")
    if not gitlab_url:
        repository = data.get("repository")
        if not repository:
            return jsonify({"message": "Missing GitLab URL"}), 400
        homepage = repository.get("homepage")
        if not homepage:
            return jsonify({"message": "Missing GitLab URL"}), 400
        try:
            parsed_url = urlparse(homepage)
            gitlab_url = f"{parsed_url.scheme}://{parsed_url.netloc}/"
        except Exception as e:
            return jsonify({"error": f"Failed to parse homepage URL: {str(e)}"}), 400

    # 优先从环境变量获取，如果没有，则从请求头获取
    gitlab_token = os.getenv("GITLAB_ACCESS_TOKEN") or request.headers.get(
        "X-Gitlab-Token"
    )
    # 如果gitlab_token为空，返回错误
    if not gitlab_token and _builtin_review_enabled():
        return jsonify({"message": "Missing GitLab access token"}), 400

    gitlab_url_slug = slugify_url(gitlab_url)

    # 打印整个payload数据，或根据需求进行处理
    logger.info(f"Received event: {object_kind}")
    logger.info(f"Payload: {json.dumps(data)}")

    # 处理Merge Request Hook
    if object_kind == "merge_request":
        # 异步触发显式配置的外部 Agent backend。
        if is_agent_review_enabled():
            handle_agent_queue(dispatch_agent_review, "gitlab", data, gitlab_url=gitlab_url)
        
        # 创建一个新进程进行异步处理
        handle_queue(
            handle_merge_request_event, data, gitlab_token, gitlab_url, gitlab_url_slug
        )
        # 立马返回响应
        return jsonify(
            {
                "message": f"Request received(object_kind={object_kind}), will process asynchronously."
            }
        ), 200
    elif object_kind == "push":
        # 创建一个新进程进行异步处理
        # TODO check if PUSH_REVIEW_ENABLED is needed here
        handle_queue(handle_push_event, data, gitlab_token, gitlab_url, gitlab_url_slug)
        # 立马返回响应
        return jsonify(
            {
                "message": f"Request received(object_kind={object_kind}), will process asynchronously."
            }
        ), 200
    else:
        error_message = f"Only merge_request and push events are supported (both Webhook and System Hook), but received: {object_kind}."
        logger.error(error_message)
        return jsonify(error_message), 400


def handle_gitea_webhook(event_type, data):
    """
    处理 Gitea Webhook
    """
    gitea_token = os.getenv("GITEA_ACCESS_TOKEN") or request.headers.get(
        "X-Gitea-Token"
    )
    if not gitea_token and _builtin_review_enabled():
        return jsonify({"message": "Missing Gitea access token"}), 400

    gitea_url = os.getenv("GITEA_URL") or "https://gitea.com"
    gitea_url_slug = slugify_url(gitea_url)

    logger.info(f"Received Gitea event: {event_type}")
    logger.info(f"Payload: {json.dumps(data)}")

    if event_type == "pull_request":
        # 异步触发显式配置的外部 Agent backend。
        if is_agent_review_enabled():
            handle_agent_queue(dispatch_agent_review, "gitea", data)
        
        handle_queue(
            handle_gitea_pull_request_event,
            data,
            gitea_token,
            gitea_url,
            gitea_url_slug,
        )
        return jsonify(
            {
                "message": f"Gitea request received(event_type={event_type}), will process asynchronously."
            }
        ), 200
    elif event_type == "push":
        handle_queue(
            handle_gitea_push_event, data, gitea_token, gitea_url, gitea_url_slug
        )
        return jsonify(
            {
                "message": f"Gitea request received(event_type={event_type}), will process asynchronously."
            }
        ), 200
    else:
        error_message = f"Only pull_request and push events are supported for Gitea webhook, but received: {event_type}."
        logger.error(error_message)
        return jsonify(error_message), 400
