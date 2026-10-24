"""多 Agent 协作 Workflow：需求 → 设计 → 代码 → 审查。

分层：
    run.py        命令行入口（交互引导、resume、结果汇总）
    workflow      流程声明（节点、条件边、并行组）
    pipeline      执行引擎（调度、并行、路由、循环、恢复）
    message       Agent 间消息总线（messages.jsonl）
    state         共享状态与节点状态机（state.json）
    agents        Agent 实现
    prompts       角色提示词
    hooks         节点专属上下文准备
    tools         确定性检查与上下文拼装
    llm           DeepSeek 客户端（requests / urllib 双后端）
    artifacts     运行目录隔离、路径安全、代码块解析
    config        配置加载
    console       终端输出
    mock          离线演示数据
"""

__version__ = "2.0.0"
__all__ = ["__version__"]
