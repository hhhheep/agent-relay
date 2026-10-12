# cross-agent-teams-channel

Claude Code channel proxy for the cross-agent-teams-mcp daemon.

## 用途

这个 plugin 是一个独立的 stdio MCP server, 作为 Claude Code 与 cross-agent-teams-mcp daemon 之间的 channel 桥接.  它声明 `capabilities.experimental['claude/channel']: {}` 以启用 Claude Code Channels 协议, 把从 daemon 收到的 `notifications/channel_wake` 中继为 `notifications/claude/channel` 发给 host Claude Code.

## 启动

```
cross-agent-teams-proxy --daemon-url http://localhost:8787 --device host-a
```

或通过环境变量 `CROSS_AGENT_TEAMS_MCP_DAEMON_URL` 提供 daemon URL.  如果 daemon 启动时带了 token, 用 `--token <t>` 或 `CROSS_AGENT_TEAMS_MCP_TOKEN` 传给 proxy.

### `--device` 的判定

`--device` 没有环境变量等价物, 只能通过 flag 传.  当 `--daemon-url` 指向非 loopback 地址时, daemon 端 `register_agent` 强制要求 device (远程缺 device 会返回 `device_required_from_remote`, 把 proxy 推入 register/fail/respawn 循环).  proxy 启动时按以下顺序决定 device:

1. 显式传了 `--device <label>` → 用该值 (经过 lowercase + `[^a-z0-9_-]` 替换为 `-` 的规范化).
2. 没传 `--device` 且 daemon 是 loopback (`127.0.0.0/8`, `localhost`, `::1`, `0.0.0.0`) → 不传 device, 让 daemon 自己用 localDevice 兜底 (零配置).
3. 没传 `--device` 但 daemon 非 loopback → 用 `os.hostname()` 自动派生一个 device label, stderr 打一行 notice 提醒.  这是兜底, 不是推荐 — hostname 派生可能与 daemon 的 localDevice 撞 (触发 `device_spoofing_local_label_from_remote`), 强烈建议跨主机部署里把 `--device` 显式写到启动配置里.
4. 没传 `--device`, daemon 非 loopback, 且 `os.hostname()` 也无法派生 (空 / 全是非法字符) → fail-fast, 退出码 2.

## Session model

- proxy 每次启动都会生成新的 `channel_session_id`, 不做持久化。
- proxy 连 daemon 时注册的是自己的 `__channel_proxy__` session, 调 `register_agent(...)` 时会同时带 `claude_ui_pid: process.ppid` 和 `delivery.channel_session_id`, daemon 把两者都写到 proxy 的 agents 行上。
- owner Claude host 需要在自己的当前 MCP session 里完成自注册。  **推荐流程**: 调用 `register_agent({ client: "claude-code", name, project_dir, ui_pid })`, 只要传了 `ui_pid` (即 Claude Code CLI 的 `$PPID`), daemon 会根据 `ui_pid = proxy.claude_ui_pid` 自动把 host 绑到 proxy 当前的 csid.  不需要从启动提示里手动读 csid。
- proxy 启动时仍会发 `notifications/claude/channel` 提示 (包含 csid + `bind_channel` 指令), 但现在只作向后兼容, 不再是完成绑定的必要环节。
- 不要用外部 `curl` 给 Claude host 代注册。  那样会新建另一个 daemon-side MCP session, 不能替代当前 Claude 会话的身份绑定。

## 安全

此 plugin 使用 Claude Code 的 Channels research preview 协议, 本地开发时需配合 `--dangerously-load-development-channels` 使用.
