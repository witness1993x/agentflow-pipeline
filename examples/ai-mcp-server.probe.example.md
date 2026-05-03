# Example: AI MCP Server Hotspot

## 1. Conclusion

`Decision`: `probe`

`One-line thesis`: AI MCP Server 是一个值得做仓库承接的热点，但当前最合理的是先验证模板路线和本地 buildability，而不是直接发布。

`Confidence`: `medium`

`Primary constraint`: `buildability`

## 2. Hotspot

- Hotspot ID: HSP-001
- Hotspot: AI MCP Server
- Original question: Should we turn the AI MCP Server hotspot into a GitHub project?
- Reframed question: 是否值得围绕 AI MCP Server 热点做一个最小可运行、可编译的 MCP server 仓库？
- Why repo: 这个热点的价值主要体现在“可运行实例”，而不是抽象讨论。
- Falsified if: 开发者最终只想看教程，不需要复用仓库。

## 3. Gate Summary

| Gate | Verdict | Score | Why it passed / held / failed |
|---|---:|---:|---|
| Reframe | pass | n/a | 这个热点适合用仓库承接 |
| Hotspot Signal | pass | 4 / 5 | 热点真实，社区活动活跃 |
| Project Shape | pass | 4 / 5 | `mcp_server` 是自然项目形态 |
| Repo Routing | hold | 3 / 5 | 还要验证 template clone 是否优于 new repo |
| Buildability | hold | 3 / 5 | 最小 build 路径还没被证明 |
| Publish Decision | hold | 3 / 5 | 现在发布还太早 |

## 4. Routing Decision

- Project shape: mcp_server
- Repo strategy: template_clone
- Candidate repo or template: minimal-mcp-template
- Why this route: 先借模板验证结构和速度，再决定是否切回 new repo

## 5. Strongest Evidence

1. MCP 相关仓库和讨论最近明显增多。
2. 这类热点更适合用一个最小可运行 repo 来承接。
3. 小团队可以快速做一次本地 build probe。

## 6. Biggest Risks

1. 模板过重，最终不如从零做。
2. 热点是热的，但仓库价值未必高。
3. 最小示例可能只能做成 demo，而不是可持续 repo。

## 7. Kill Signals

- 如果模板存在隐藏依赖导致本地无法快速跑通。
- 如果最终只能做成一次性演示，无法解释 repo 的公开价值。
- 如果热点快速退潮，项目时效性显著下降。

`Veto from gate`: `Buildability`

## 8. Build / Publish Logic

### Why this may work

- 热点与可运行示例之间天然匹配。
- 探针成本低，能快速验证路线。

### Why this may fail

- 模板路线可能让仓库价值被上游吞没。
- build 复杂度可能高于预期。

### Why now / why not now

- 现在适合 probe，因为窗口仍在。
- 现在不适合 publish，因为 repo 路线和 buildability 还没站稳。

## 9. Next Step

`Immediate action`: `run build probe`

`Build command`: `npm run build`

`Test command`: `npm test`

`Timebox`: `1 day`

`Owner`: `founder`

`Success signal`: 本地 build 和最小测试通过

`Failure signal`: 模板存在隐藏依赖或改造成本过高

`Next review date`: `2026-05-04`

## 10. Review Delta

`Previous status`: `watch`

`What changed since last round`: 现在已经收敛到具体的 repo 承接方向和 probe 动作。

`What remains unresolved`: template_clone 是否优于 new_repo。

`Lesson so far`: 热点足够强并不等于可以直接发布，repo 路由和 buildability 是关键中间层。
