# Qdrant Retrieval Port Adapter

该适配器实现 Retrieval Port 1.0 的 `semantic_search` 能力。它是可选增强，
不会取代本地 Plan、Evidence、Closeout 或 ADR-0002 Context Pack。

## 显式来源

消费项目必须在 `vibe-workflow.json` 声明允许文件：

```json
{
  "schema_version": "1.0",
  "steps": [],
  "retrieval": {
    "contract_version": "1.0",
    "sources": [
      {"source_id": "decisions", "path": "knowledge/decisions.md", "media_type": "text/markdown"}
    ]
  }
}
```

框架核心解析这些具体文件并向索引器传递只读逻辑记录。适配器不接收
`project_root`，不扫描目录、glob、源码或隐式默认根。未声明 `retrieval`
时，索引和查询均在模型加载与网络调用前失败关闭。

支持的媒体类型是 `text/markdown`、`application/json` 和 `text/plain`。
JSON 在核心边界规范化；单文件最多 4,000,000 字节。

## 索引

`--dry-run` 只解析显式来源并生成确定性分块计划，不导入 FastEmbed、
不连接 Qdrant、不写消费项目：

```text
ccgs qdrant-index --project-root <project> --project-id <id> --dry-run
```

显式 `--write` 才会加载模型并同步远程集合。Point payload 使用
`project_id`、`source_id`、`source_path` 和 `media_type` 保存隔离身份，
并符合 `schemas/semantic-index-point.schema.json`。

## 查询

查询同样必须显式选择安全模式：

```text
ccgs qdrant-query --project-root <project> --project-id <id> \
  --request-id <request> --source-id decisions --query "decision" --dry-run
```

`--dry-run` 只验证 Manifest、Request、Capability 和 Port 契约，零模型、
零网络调用。`--write` 恰好调用一次注入的适配器。远程查询同时使用精确
`project_id` 和所选 `source_id` 过滤；包装器先验证原始 Point 身份与字段，
核心再按 Manifest 执行来源映射后验校验。

## 安全边界

- 适配器不拥有状态机、策略、Evidence、Closeout 或 Context Pack 写入口。
- 结果只包含规范字段，最多 50 项、每段文本最多 2400 字符。
- 绝对路径、凭据和敏感正文返回 `PORT_PAYLOAD_UNSAFE`，不回显拒绝值。
- 错误项目、来源、路径映射和字段结构返回 `PORT_PROTOCOL_INVALID`。
- 只有已调用后的传输不可用和超时可重试。
- API Key 只能来自环境变量；非回环 HTTP 默认拒绝。
