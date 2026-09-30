"""
API 服务主程序入口
"""
from biz.utils.environment import load_project_environment

# 必须在其他导入之前加载环境变量
load_project_environment()

import os

from biz.api import api_app, init_app
from biz.api.scheduler import setup_scheduler
from biz.utils.config_checker import check_config

# 初始化应用并注册路由
init_app(api_app)

if __name__ == '__main__':
    check_config()
    # 启动定时任务调度器
    setup_scheduler()

    # 启动Flask API服务
    port = int(os.environ.get('SERVER_PORT', 5001))
    api_app.run(host='0.0.0.0', port=port)
