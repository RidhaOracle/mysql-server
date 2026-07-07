# Mark Integrate Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a GitHub Actions workflow that keeps the `integrate` label synchronized with active pull-request approvals.

**Architecture:** A `pull_request_review` workflow reconciles all review history after submitted or dismissed reviews. It keeps each reviewer's latest meaningful state, then adds the label when any reviewer is currently approved and removes it otherwise.

**Tech Stack:** GitHub Actions YAML, `actions/github-script@v7`, JavaScript, Ruby YAML/Open3 test harness

---

### Task 1: Add and test review-state reconciliation

**Files:**
- Create: `.github/workflows/mark-integrate.yml`
- Test temporarily: `/private/tmp/mark-integrate-workflow-test.rb`

- [ ] **Step 1: Create the temporary behavior harness**

Create `/private/tmp/mark-integrate-workflow-test.rb` with:

```ruby
require "json"
require "open3"
require "yaml"

workflow = ARGV.fetch(0, ".github/workflows/mark-integrate.yml")
script = YAML.load_file(workflow)
             .fetch("jobs")
             .fetch("integrate")
             .fetch("steps")
             .fetch(0)
             .fetch("with")
             .fetch("script")

cases = {
  "no reviews" => [[], "remove"],
  "one approval" => [
    [{ user: { login: "alice" }, state: "APPROVED" }],
    "add"
  ],
  "comment after approval" => [
    [
      { user: { login: "alice" }, state: "APPROVED" },
      { user: { login: "alice" }, state: "COMMENTED" }
    ],
    "add"
  ],
  "changes requested after approval" => [
    [
      { user: { login: "alice" }, state: "APPROVED" },
      { user: { login: "alice" }, state: "CHANGES_REQUESTED" }
    ],
    "remove"
  ],
  "dismissed approval" => [
    [
      { user: { login: "alice" }, state: "APPROVED" },
      { user: { login: "alice" }, state: "DISMISSED" }
    ],
    "remove"
  ],
  "another reviewer remains approved" => [
    [
      { user: { login: "alice" }, state: "APPROVED" },
      { user: { login: "bob" }, state: "DISMISSED" }
    ],
    "add"
  ]
}

cases.each do |name, (reviews, expected)|
  prelude = <<~JS
    const calls = [];
    const reviews = #{JSON.generate(reviews)};
    const github = {
      paginate: async () => reviews,
      rest: {
        pulls: { listReviews: () => {} },
        issues: {
          getLabel: async () => ({}),
          updateLabel: async (args) => calls.push({ op: "update", args }),
          createLabel: async (args) => calls.push({ op: "create", args }),
          addLabels: async (args) => calls.push({ op: "add", args }),
          removeLabel: async (args) => calls.push({ op: "remove", args }),
        },
      },
    };
    const context = {
      repo: { owner: "RidhaOracle", repo: "mysql-server" },
      payload: { pull_request: { number: 1 } },
    };
    const core = { info: () => {} };
  JS

  program = <<~JS
    #{prelude}
    (async () => {
      #{script}
      const action = calls.find(({ op }) => op === "add" || op === "remove");
      process.stdout.write(action?.op || "none");
    })().catch((error) => {
      console.error(error);
      process.exit(1);
    });
  JS

  stdout, stderr, status = Open3.capture3("node", "-", stdin_data: program)
  abort "#{name}: #{stderr}" unless status.success?
  abort "#{name}: expected #{expected}, got #{stdout}" unless stdout == expected
end

puts "#{cases.length} review-state scenarios passed"
```

- [ ] **Step 2: Run the harness and verify RED**

Run:

```bash
ruby /private/tmp/mark-integrate-workflow-test.rb
```

Expected: failure with `No such file or directory @ rb_sysopen - .github/workflows/mark-integrate.yml`.

- [ ] **Step 3: Create the workflow**

Create `.github/workflows/mark-integrate.yml` with:

```yaml
name: Mark Integrate

on:
  pull_request_review:
    types: [submitted, dismissed]

permissions:
  contents: read
  issues: write
  pull-requests: write

concurrency:
  group: mark-integrate-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  integrate:
    runs-on: ubuntu-24.04
    steps:
      - name: Reconcile integrate label
        uses: actions/github-script@v7
        with:
          script: |
            const pr = context.payload.pull_request;
            const label = {
              name: 'integrate',
              color: '5319E7',
              description: 'Approved patch ready for integration',
            };

            try {
              await github.rest.issues.getLabel({ ...context.repo, name: label.name });
              await github.rest.issues.updateLabel({ ...context.repo, ...label });
            } catch (error) {
              if (error.status !== 404) throw error;
              await github.rest.issues.createLabel({ ...context.repo, ...label });
            }

            const reviews = await github.paginate(github.rest.pulls.listReviews, {
              ...context.repo,
              pull_number: pr.number,
              per_page: 100,
            });
            const meaningfulStates = new Set(['APPROVED', 'CHANGES_REQUESTED', 'DISMISSED']);
            const latestStates = new Map();

            for (const review of reviews) {
              const login = review.user?.login;
              if (login && meaningfulStates.has(review.state)) {
                latestStates.set(login, review.state);
              }
            }

            const approvers = [...latestStates.entries()]
              .filter(([, state]) => state === 'APPROVED')
              .map(([login]) => login);

            if (approvers.length > 0) {
              await github.rest.issues.addLabels({
                ...context.repo,
                issue_number: pr.number,
                labels: [label.name],
              });
              core.info(`Marked PR #${pr.number} as integrate; active approvals: ${approvers.join(', ')}.`);
            } else {
              try {
                await github.rest.issues.removeLabel({
                  ...context.repo,
                  issue_number: pr.number,
                  name: label.name,
                });
              } catch (error) {
                if (error.status !== 404) throw error;
              }
              core.info(`Removed integrate from PR #${pr.number}; no active approvals remain.`);
            }
```

- [ ] **Step 4: Run the behavior harness and verify GREEN**

Run:

```bash
ruby /private/tmp/mark-integrate-workflow-test.rb
```

Expected: `6 review-state scenarios passed`.

- [ ] **Step 5: Validate YAML and embedded JavaScript syntax**

Run:

```bash
ruby -e 'require "yaml"; require "open3"; file=".github/workflows/mark-integrate.yml"; doc=YAML.load_file(file); script=doc.fetch("jobs").fetch("integrate").fetch("steps").fetch(0).fetch("with").fetch("script"); _, err, status=Open3.capture3("node", "--check", "-", stdin_data: "async function run() {\n#{script}\n}\n"); abort err unless status.success?; puts "workflow yaml and script syntax ok"'
```

Expected: `workflow yaml and script syntax ok`.

- [ ] **Step 6: Remove the temporary test harness**

Delete `/private/tmp/mark-integrate-workflow-test.rb`.

- [ ] **Step 7: Commit the workflow**

```bash
git add .github/workflows/mark-integrate.yml
git commit -m "Add integrate label workflow"
```

### Task 2: Verify the complete GitHub Actions configuration

**Files:**
- Verify: `.github/workflows/*.yml`
- Verify: `.github/workflows/mark-integrate.yml`

- [ ] **Step 1: Parse every workflow**

Run:

```bash
ruby -e 'require "yaml"; files=Dir[".github/workflows/*.{yml,yaml}"]; files.each { |file| YAML.load_file(file) }; puts "parsed #{files.length} workflows"'
```

Expected: `parsed 8 workflows`.

- [ ] **Step 2: Verify required workflow configuration**

Run:

```bash
rg -n -F 'types: [submitted, dismissed]' .github/workflows/mark-integrate.yml
rg -n -F 'group: mark-integrate-${{ github.event.pull_request.number }}' .github/workflows/mark-integrate.yml
rg -n -F 'uses: actions/github-script@v7' .github/workflows/mark-integrate.yml
rg -n -F "meaningfulStates = new Set(['APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'])" .github/workflows/mark-integrate.yml
```

Expected: each command prints exactly one matching line.

- [ ] **Step 3: Inspect the final diff and status**

Run:

```bash
git diff --check 5a0bfa48ccc..HEAD -- .github/workflows/mark-integrate.yml
git diff --stat 5a0bfa48ccc..HEAD -- .github/workflows/mark-integrate.yml
git diff 5a0bfa48ccc..HEAD -- .github/workflows/mark-integrate.yml
git status --short
```

Expected: no whitespace errors; the implementation diff contains only the new workflow; status contains only the user's pre-existing untracked `.DS_Store` files.
