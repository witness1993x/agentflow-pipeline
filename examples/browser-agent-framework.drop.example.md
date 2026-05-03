# Example: Browser Agent Framework Hotspot

## 1. Conclusion

`Decision`: `drop`

`One-line thesis`: Browser Agent Framework 虽然是大热点，但对当前团队来说过于宽泛，repo 路由和差异化都不成立，不值得继续投入。

`Confidence`: `medium`

`Primary constraint`: `repo_routing`

## 2. Hotspot

- Hotspot ID: HSP-002
- Hotspot: Browser Agent Framework
- Original question: Should we turn the Browser Agent Framework hotspot into a GitHub project?
- Reframed question: 小团队是否有机会在 Browser Agent Framework 方向找到一个值得公开发布的清晰仓库切口？
- Why repo: 只有当切口足够具体时，仓库才有意义。
- Falsified if: 能找到一个强垂直场景和清晰 repo 形态。

## 3. Gate Summary

| Gate | Verdict | Score | Why it passed / held / failed |
|---|---:|---:|---|
| Reframe | pass | n/a | 问题已经从“追热点”收敛到“找仓库切口” |
| Hotspot Signal | pass | 4 / 5 | 热点本身是真实的 |
| Project Shape | hold | 2 / 5 | 项目形态过于发散 |
| Repo Routing | fail | 1 / 5 | 无论 fork、template 还是 new repo 都没有明显优解 |
| Buildability | hold | 2 / 5 | 还没进入值得验证的程度 |
| Publish Decision | fail | 1 / 5 | 现在发布几乎没有意义 |

## 4. Routing Decision

- Project shape: undecided
- Repo strategy: new_repo
- Candidate repo or template: none
- Why this route: 当前没有适合 fork 或 template 的清晰上游，但 new repo 也缺少可赢切口

## 5. Strongest Evidence

1. Browser agent 方向确实持续升温。
2. 社区里有很多演示和框架出现。
3. 这个方向天然吸引注意力。

## 6. Biggest Risks

1. 切口过宽，没有差异化。
2. 平台或更大玩家会快速内置类似能力。
3. 小团队难以长期维护一个泛化框架仓库。

## 7. Kill Signals

- 如果始终无法收敛到一个垂直可发布项目。
- 如果仓库价值只能停留在 demo 层。
- 如果路由策略始终没有明显优解。

`Veto from gate`: `Repo Routing`

## 8. Build / Publish Logic

### Why this may work

- 如果以后出现强垂直切口，仍可能重开。
- 热点本身有足够关注度。

### Why this may fail

- 热点大不等于仓库值得做。
- 没有清晰 repo route，技术验证也没有意义。

### Why now / why not now

- 现在适合继续观察大方向。
- 现在不适合为当前团队建仓库。

## 9. Next Step

`Immediate action`: `stop`

`Build command`:

`Test command`:

`Timebox`: `0 days`

`Owner`: `founder`

`Success signal`: 未来若出现明确垂直场景，再重新进入 intake

`Failure signal`: 继续停留在宽泛叙事、无仓库切口

`Next review date`:

## 10. Review Delta

`Previous status`: `watch`

`What changed since last round`: 更明确地识别出“没有 repo 路线”才是根本问题。

`What remains unresolved`: 是否存在值得单独切出的垂直框架仓库。

`Lesson so far`: 不是每个热点都应该被做成 GitHub 项目。
