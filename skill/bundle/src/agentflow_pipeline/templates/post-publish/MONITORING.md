# Post-publish monitoring

Lightweight monitoring plan for `{{ github_owner }}/{{ repo_name }}` after the initial publish. Goal: catch infra regressions and community signal early without setting up a dedicated observability stack.

## 1. Chainstream credits monitor
Goal: detect when on-chain or data-probe credits drop below the safety threshold so the pipeline does not silently degrade.

- Cadence: daily.
- Implementation: small cron job (GitHub Actions `schedule:` or local launchd) that calls the Chainstream balance endpoint and writes the value to a status file.
- Alert: if remaining credits fall below 20 percent of the monthly budget, open a GitHub issue tagged `ops` in this repo.
- Owner: `@{{ github_owner }}`.

## 2. GitHub stars and traffic
Goal: know when growth changes shape, without paying for analytics.

- Use the built-in `Insights -> Traffic` page weekly for clones, views, and referrers.
- Subscribe to repository activity via Watch -> Releases and discussions only, to avoid noise.
- Optional: a weekly GitHub Action that posts star delta into `discussions` or a Slack webhook.

## 3. Issue and PR alerting
Goal: no opened issue waits more than 48 hours without acknowledgement.

- Configure repository notifications: `Settings -> Notifications -> Custom` to receive every new issue.
- Add a SLA reminder Action (e.g. `actions-cool/issues-helper`) that comments on issues idle for more than 48 hours.
- Maintain a `triage` label so incoming reports get sorted before deeper review.

## 4. CI health
Goal: surface flaky or broken builds quickly.

- Default CI workflow is `.github/workflows/ci.yml`.
- Treat any red `{{ default_branch }}` build as a blocker; revert the offending commit if a fix is not landable within an hour.
- Track failure rate manually for the first month; revisit if it exceeds 10 percent of runs.

## 5. Escalation
- Code or build issues: open issue, ping `@{{ github_owner }}`.
- Credits or external service outage: escalate via the project's primary chat channel.
- Security: follow `SECURITY.md` if present, otherwise email the owner directly.
