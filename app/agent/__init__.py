"""智能体这一层：工具（tools.py）、上下文（context.py）、提示词（prompt.py）与
hooks（hooks.py），runner（Task 4）。

包本身什么都不导入（与 app/wiki、app/services 的空 __init__ 同一个口径）：
`app.agent.tools` 与 `app.agent.hooks` 在模块级拉 openai-agents，只有真要跑智能体的
那条路（runner、测试）才该付这份导入开销。`context.py` 与 `prompt.py` 不拉 SDK。
"""
