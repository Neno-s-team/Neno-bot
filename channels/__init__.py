"""简单通道：不用编译任何东西就能跟她说话。

- terminal      终端里直接聊（什么账号都不要，装好就能试）
- telegram_bot  Telegram 机器人（@BotFather 要一个 token 就行）

它们都只做三件事：把你的话交给后端（POST /chat）、取到点该发的（GET /mind/deliveries/due）、
发出去后回执（POST /mind/deliveries/{id}/ack）。她什么时候看到、想多久、分几条发、主动找你，都是后端的事。
所有简单通道共用一个会话（MIND_HOME_SESSION，默认 me），所以换通道她还记得你。

用真人号的 Telegram 网关（TDLib）在 telegram_gateway/，那个要编译，见 docs。
"""
