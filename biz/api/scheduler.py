"""
定时任务调度器模块
"""
import atexit
import os
import traceback
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from biz.utils.log import logger
from biz.api.routes.daily_report import daily_report_task
from biz.agent.config import is_agent_review_enabled, load_agent_review_config
from biz.agent.service import reap_agent_review_workspaces


def setup_scheduler():
    """
    配置并启动定时任务调度器
    """
    try:
        scheduler = BackgroundScheduler()
        crontab_expression = os.getenv('REPORT_CRONTAB_EXPRESSION', '0 18 * * 1-5')
        cron_parts = crontab_expression.split()
        cron_minute, cron_hour, cron_day, cron_month, cron_day_of_week = cron_parts

        # Schedule the task based on the crontab expression
        scheduler.add_job(
            daily_report_task,
            trigger=CronTrigger(
                minute=cron_minute,
                hour=cron_hour,
                day=cron_day,
                month=cron_month,
                day_of_week=cron_day_of_week
            )
        )

        if is_agent_review_enabled():
            lease_seconds = load_agent_review_config().job_lease_seconds
            scheduler.add_job(
                reap_agent_review_workspaces,
                trigger=IntervalTrigger(seconds=max(60, lease_seconds // 2)),
                id="agent-review-workspace-reaper",
                replace_existing=True,
            )

        # Start the scheduler
        scheduler.start()
        logger.info("Scheduler started successfully.")

        # Shut down the scheduler when exiting the app
        atexit.register(lambda: scheduler.shutdown())
    except Exception as e:
        logger.error(f"Error setting up scheduler: {e}")
        logger.error(traceback.format_exc())
