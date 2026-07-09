# Assign Code Owners Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Request the two wildcard code owners after OCA verification and mark the pull request as `Review Requested`.

**Architecture:** A pull-request workflow reads the wildcard owners from CODEOWNERS at a temporary test ref, filters the author and existing requests, requests remaining reviewers, and labels the PR only after assignment succeeds. CODEOWNERS is simplified to one wildcard rule so native ownership agrees with the workflow.

**Tech Stack:** GitHub Actions YAML, `actions/github-script@v7`, JavaScript, Ruby YAML/Open3 test harness

---

### Task 1: Add test-first reviewer assignment

**Files:**
- Create: `.github/workflows/assign-codeowners.yml`
- Modify: `.github/CODEOWNERS`
- Test temporarily: `/private/tmp/assign-codeowners-workflow-test.rb`

- [ ] **Step 1: Create the temporary workflow harness**

Create `/private/tmp/assign-codeowners-workflow-test.rb` with:

```ruby
require "base64"
require "json"
require "open3"
require "yaml"

script = YAML.load_file(".github/workflows/assign-codeowners.yml")
             .fetch("jobs")
             .fetch("assign")
             .fetch("steps")
             .fetch(0)
             .fetch("with")
             .fetch("script")

owners = "* @seemasundara @gopshank\n"
cases = [
  {
    name: "requests both wildcard owners",
    codeowners: owners,
    author: "RidhaOracle",
    requested: [],
    expected: %w[seemasundara gopshank],
    label: true
  },
  {
    name: "excludes the pull request author",
    codeowners: owners,
    author: "seemasundara",
    requested: [],
    expected: %w[gopshank],
    label: true
  },
  {
    name: "excludes an existing request",
    codeowners: owners,
    author: "RidhaOracle",
    requested: %w[seemasundara],
    expected: %w[gopshank],
    label: true
  },
  {
    name: "labels when all owners are already requested",
    codeowners: owners,
    author: "RidhaOracle",
    requested: %w[seemasundara gopshank],
    expected: nil,
    label: true
  },
  {
    name: "fails without a wildcard rule",
    codeowners: "/storage/innobase/ @someone\n",
    author: "RidhaOracle",
    requested: [],
    expected: nil,
    label: false,
    error: true
  },
  {
    name: "does not label after request failure",
    codeowners: owners,
    author: "RidhaOracle",
    requested: [],
    expected: %w[seemasundara gopshank],
    label: false,
    request_error: true,
    error: true
  }
]

cases.each do |test|
  scenario = JSON.generate(test)
  program = <<~JS
    const scenario = #{scenario};
    const calls = [];
    const github = {
      rest: {
        repos: {
          getContent: async (args) => {
            calls.push({ op: 'content', args });
            return {
              data: {
                type: 'file',
                content: Buffer.from(scenario.codeowners).toString('base64'),
              },
            };
          },
        },
        pulls: {
          get: async (args) => {
            calls.push({ op: 'get-pr', args });
            return {
              data: {
                requested_reviewers: scenario.requested.map((login) => ({ login })),
              },
            };
          },
          requestReviewers: async (args) => {
            calls.push({ op: 'request', args });
            if (scenario.request_error) throw new Error('request failed');
          },
        },
        issues: {
          getLabel: async (args) => calls.push({ op: 'get-label', args }),
          updateLabel: async (args) => calls.push({ op: 'update-label', args }),
          createLabel: async (args) => calls.push({ op: 'create-label', args }),
          addLabels: async (args) => calls.push({ op: 'add-label', args }),
        },
      },
    };
    const context = {
      repo: { owner: 'RidhaOracle', repo: 'mysql-server' },
      payload: {
        pull_request: {
          number: 1,
          user: { login: scenario.author },
        },
      },
    };
    const core = { info: () => {} };

    (async () => {
      try {
        #{script}
        process.stdout.write(JSON.stringify({ ok: true, calls }));
      } catch (error) {
        process.stdout.write(JSON.stringify({ ok: false, error: error.message, calls }));
      }
    })();
  JS

  stdout, stderr, status = Open3.capture3("node", "-", stdin_data: program)
  abort "#{test[:name]}: #{stderr}" unless status.success?
  result = JSON.parse(stdout)
  abort "#{test[:name]}: unexpected result #{result}" unless result["ok"] == !test[:error]

  request = result["calls"].find { |call| call["op"] == "request" }
  actual = request&.dig("args", "reviewers")
  abort "#{test[:name]}: expected #{test[:expected]}, got #{actual}" unless actual == test[:expected]

  add_index = result["calls"].index { |call| call["op"] == "add-label" }
  if test[:label]
    abort "#{test[:name]}: Review Requested label missing" unless add_index
    request_index = result["calls"].index { |call| call["op"] == "request" }
    abort "#{test[:name]}: label preceded request" if request_index && add_index < request_index
  else
    abort "#{test[:name]}: label added after failure" if add_index
  end
end

puts "#{cases.length} reviewer-assignment scenarios passed"
```

- [ ] **Step 2: Run the harness and verify RED**

Run:

```bash
ruby /private/tmp/assign-codeowners-workflow-test.rb
```

Expected: failure because `.github/workflows/assign-codeowners.yml` does not exist.

- [ ] **Step 3: Create the assignment workflow**

Create `.github/workflows/assign-codeowners.yml` with:

```yaml
name: Assign Code Owners

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review, labeled]

permissions:
  contents: read
  issues: write
  pull-requests: write

concurrency:
  group: assign-codeowners-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  assign:
    if: ${{ !github.event.pull_request.draft && contains(github.event.pull_request.labels.*.name, 'OCA Verified') }}
    runs-on: ubuntu-24.04
    steps:
      - name: Request review from code owners
        uses: actions/github-script@v7
        with:
          script: |
            const pr = context.payload.pull_request;
            // TODO: Before merge, change this test ref to 'trunk'.
            const codeownersRef = 'devex/streamline';
            const path = '.github/CODEOWNERS';

            const response = await github.rest.repos.getContent({
              ...context.repo,
              path,
              ref: codeownersRef,
            });
            if (Array.isArray(response.data) || response.data.type !== 'file' || !response.data.content) {
              throw new Error(`${path} at ${codeownersRef} is not a readable file`);
            }

            const codeowners = Buffer.from(response.data.content, 'base64').toString('utf8');
            const defaultLine = codeowners
              .split(/\r?\n/)
              .map((line) => line.replace(/\s+#.*$/, '').trim())
              .find((line) => line && !line.startsWith('#') && line.split(/\s+/)[0] === '*');
            if (!defaultLine) {
              throw new Error(`No wildcard owner rule found in ${path} at ${codeownersRef}`);
            }

            const owners = [...new Set(
              defaultLine
                .split(/\s+/)
                .slice(1)
                .filter((owner) => owner.startsWith('@'))
                .map((owner) => owner.slice(1))
                .filter(Boolean),
            )];
            if (owners.length === 0) {
              throw new Error(`Wildcard rule in ${path} has no GitHub user owners`);
            }

            const current = await github.rest.pulls.get({
              ...context.repo,
              pull_number: pr.number,
            });
            const author = pr.user.login.toLowerCase();
            const existing = new Set(
              current.data.requested_reviewers.map((reviewer) => reviewer.login.toLowerCase()),
            );
            const eligible = owners.filter((owner) => owner.toLowerCase() !== author);
            if (eligible.length === 0) {
              throw new Error('No eligible code owners remain after excluding the pull request author');
            }

            const reviewers = eligible.filter((owner) => !existing.has(owner.toLowerCase()));
            if (reviewers.length > 0) {
              await github.rest.pulls.requestReviewers({
                ...context.repo,
                pull_number: pr.number,
                reviewers,
              });
            }

            const label = {
              name: 'Review Requested',
              color: '1D76DB',
              description: 'Review requested from code owners',
            };
            try {
              await github.rest.issues.getLabel({ ...context.repo, name: label.name });
              await github.rest.issues.updateLabel({ ...context.repo, ...label });
            } catch (error) {
              if (error.status !== 404) throw error;
              await github.rest.issues.createLabel({ ...context.repo, ...label });
            }
            await github.rest.issues.addLabels({
              ...context.repo,
              issue_number: pr.number,
              labels: [label.name],
            });

            const assigned = reviewers.length > 0 ? reviewers : eligible;
            core.info(`Review requested from ${assigned.map((owner) => `@${owner}`).join(', ')}.`);
```

- [ ] **Step 4: Simplify CODEOWNERS**

Replace `.github/CODEOWNERS` with:

```text
# Temporary default owners for every path. Replace this wildcard with
# path-specific teams as the external committer model rolls out.
* @seemasundara @gopshank
```

- [ ] **Step 5: Run the behavior harness and verify GREEN**

Run:

```bash
ruby /private/tmp/assign-codeowners-workflow-test.rb
```

Expected: `6 reviewer-assignment scenarios passed`.

- [ ] **Step 6: Validate YAML and embedded JavaScript**

Run:

```bash
ruby -e 'require "yaml"; require "open3"; doc=YAML.load_file(".github/workflows/assign-codeowners.yml"); script=doc.fetch("jobs").fetch("assign").fetch("steps").fetch(0).fetch("with").fetch("script"); _, err, status=Open3.capture3("node", "--check", "-", stdin_data: "async function run() {\n#{script}\n}\n"); abort err unless status.success?; puts "workflow yaml and script syntax ok"'
```

Expected: `workflow yaml and script syntax ok`.

- [ ] **Step 7: Remove the temporary harness**

Delete `/private/tmp/assign-codeowners-workflow-test.rb`.

- [ ] **Step 8: Commit the implementation**

```bash
git add .github/workflows/assign-codeowners.yml .github/CODEOWNERS
git commit -m "Assign reviewers after OCA verification"
```

### Task 2: Verify the complete change

**Files:**
- Verify: `.github/workflows/*.yml`
- Verify: `.github/workflows/assign-codeowners.yml`
- Verify: `.github/CODEOWNERS`

- [ ] **Step 1: Parse all workflows**

Run:

```bash
ruby -e 'require "yaml"; files=Dir[".github/workflows/*.{yml,yaml}"]; files.each { |file| YAML.load_file(file) }; puts "parsed #{files.length} workflows"'
```

Expected: `parsed 9 workflows`.

- [ ] **Step 2: Verify the gate, temporary ref, TODO, and label**

Run:

```bash
rg -n -F "contains(github.event.pull_request.labels.*.name, 'OCA Verified')" .github/workflows/assign-codeowners.yml
rg -n -F "const codeownersRef = 'devex/streamline';" .github/workflows/assign-codeowners.yml
rg -n -F "TODO: Before merge, change this test ref to 'trunk'." .github/workflows/assign-codeowners.yml
rg -n -F "name: 'Review Requested'" .github/workflows/assign-codeowners.yml
grep -qxF '* @seemasundara @gopshank' .github/CODEOWNERS
test "$(grep -c '^[^#]' .github/CODEOWNERS)" -eq 1
```

Expected: every assertion passes and each search prints one matching line.

- [ ] **Step 3: Inspect the implementation diff and status**

Run:

```bash
git diff --check 93566e5c31e..HEAD -- .github/workflows/assign-codeowners.yml .github/CODEOWNERS
git diff --stat 93566e5c31e..HEAD -- .github/workflows/assign-codeowners.yml .github/CODEOWNERS
git diff 93566e5c31e..HEAD -- .github/workflows/assign-codeowners.yml .github/CODEOWNERS
git status --short
```

Expected: no whitespace errors; the implementation diff contains only the new workflow and CODEOWNERS simplification; status contains only the user's pre-existing untracked `.DS_Store` files.
