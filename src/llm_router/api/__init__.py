"""请求管线 ①门卫→②匹配→③路由→④回退。S1.x/S2.x 切片填充。
- policy_enforcer(① S2.7 合规)/ matcher(② S2.2/S2.9)/ strategy(③ S1.3/S2.1)/ cascade(④ S2.1)
"""
