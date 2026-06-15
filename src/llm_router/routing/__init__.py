"""routing 子包 — 路由决策归因(hop 语义、能力匹配等)。

S1.5a 引入 hop.py(hop=conditional 边界跳变 + total_retry_budget 约束)。
与持久化层(store/)、韧性层(resilience/)解耦:本包只做纯计算决策归因。
"""
