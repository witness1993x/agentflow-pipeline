# Example: Vertical SDK Hotspot

## 1. Conclusion

`Decision`: `publish`

`One-line thesis`: 某个垂直行业 SDK 热点已经同时满足清晰项目形态、明确 repo 路由和可验证的 buildability，适合正式发布 GitHub 仓库。

`Confidence`: `high`

`Primary constraint`: `unknown`

## 2. Hotspot

- Hotspot ID: HSP-003
- Hotspot: Vertical SDK
- Original question: Should we turn the Vertical SDK hotspot into a GitHub project?
- Reframed question: 在已有行业接口和早期开发者需求的基础上，是否应该把垂直 SDK 正式发布成 GitHub 仓库？
- Why repo: SDK 的价值天然依赖公开 repo、安装方式、示例和测试。
- Falsified if: 目标开发者并不需要公开 SDK，而更依赖内部集成服务。

## 3. Gate Summary

| Gate | Verdict | Score | Why it passed / held / failed |
|---|---:|---:|---|
| Reframe | pass | n/a | 这是天然适合以 GitHub 仓库承接的项目 |
| Hotspot Signal | pass | 4 / 5 | 热点真实且有明确开发者需求 |
| Project Shape | pass | 5 / 5 | `sdk` 是明确且稳定的项目形态 |
| Repo Routing | pass | 4 / 5 | `new_repo` 明显优于 fork/template |
| Buildability | pass | 4 / 5 | 已有可运行 build/test 路径 |
| Publish Decision | pass | 5 / 5 | 仓库已达到最低公开标准 |

## 4. Routing Decision

- Project shape: sdk
- Repo strategy: new_repo
- Candidate repo or template: none
- Why this route: 需要清晰、可控的公共接口，不适合依赖已有上游结构

## 5. Strongest Evidence

1. 已有开发者需求明确指向公开 SDK。
2. 本地 build、test 和示例路径已经跑通。
3. 项目天然适合通过 GitHub 分发。

## 6. Biggest Risks

1. 发布后需要持续维护版本与示例。
2. 如果接口定义频繁变化，会提高维护成本。
3. 需要确保 README 和安装路径足够清楚。

## 7. Kill Signals

- 如果公开接口仍然不稳定。
- 如果测试不能覆盖最小主流程。
- 如果没有最小示例支撑开发者上手。

`Veto from gate`: `none`

## 8. Build / Publish Logic

### Why this may work

- 项目形态清晰、公开价值强、开发者受众明确。
- 从零建仓库能最大化接口可控性和长期维护性。

### Why this may fail

- 发布只是开始，维护能力必须跟上。
- 如果 README 与示例不到位，会削弱公开仓库价值。

### Why now / why not now

- 现在适合 publish，因为关键中间层都已验证通过。
- 继续停留在 probe 会浪费窗口和复用价值。

## 9. Next Step

`Immediate action`: `publish repo`

`Build command`: `make build`

`Test command`: `make test`

`Timebox`: `3 days`

`Owner`: `founder`

`Success signal`: 仓库公开后，开发者能按 README 完成安装、运行和测试

`Failure signal`: 发布后仍需要大量口头补充说明才能使用

`Next review date`: `2026-05-04`

## 10. Review Delta

`Previous status`: `probe`

`What changed since last round`: build、test、示例和 repo 路线都已经验证完成。

`What remains unresolved`: 发布后的持续维护节奏。

`Lesson so far`: 当项目形态和构建路径都稳定时，正式发布比继续犹豫更有价值。
