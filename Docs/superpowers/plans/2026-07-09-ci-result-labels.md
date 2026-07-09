# CI Result Labels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Label pull requests with the aggregate PR Build and MTR Smoke result.

**Architecture:** Each existing workflow gains one reporter job that depends on the complete build/test job, runs for definitive success or failure, and atomically replaces the opposite result label. The build matrix remains untouched and reports through one aggregate job.

**Tech Stack:** GitHub Actions YAML, `actions/github-script@v7`, JavaScript, Ruby YAML/Open3 test harness

---

### Task 1: Add aggregate CI result reporters

**Files:**
- Modify: `.github/workflows/pr-build.yml`
- Modify: `.github/workflows/mtr-smoke.yml`
- Test temporarily: `/private/tmp/ci-result-labels-test.rb`

- [ ] **Step 1: Create the temporary behavior harness**

Create `/private/tmp/ci-result-labels-test.rb` with:

```ruby
require "json"
require "open3"
require "yaml"

workflows = [
  {
    file: ".github/workflows/pr-build.yml",
    need: "build",
    passed: "Build Passed",
    failed: "Build Failed"
  },
  {
    file: ".github/workflows/mtr-smoke.yml",
    need: "smoke",
    passed: "MTR Passed",
    failed: "MTR Failed"
  }
]

workflows.each do |workflow|
  script = YAML.load_file(workflow[:file])
               .fetch("jobs")
               .fetch("report")
               .fetch("steps")
               .fetch(0)
               .fetch("with")
               .fetch("script")

  %w[success failure].each do |result|
    executable = script.gsub("${{ needs.#{workflow[:need]}.result }}", result)
    program = <<~JS
      const calls = [];
      const github = {
        rest: {
          issues: {
            getLabel: async (args) => calls.push({ op: 'get', args }),
            updateLabel: async (args) => calls.push({ op: 'update', args }),
            createLabel: async (args) => calls.push({ op: 'create', args }),
            removeLabel: async (args) => calls.push({ op: 'remove', args }),
            addLabels: async (args) => calls.push({ op: 'add', args }),
          },
        },
      };
      const context = {
        repo: { owner: 'RidhaOracle', repo: 'mysql-server' },
        payload: { pull_request: { number: 1 } },
      };
      const core = { info: () => {} };

      (async () => {
        #{executable}
        process.stdout.write(JSON.stringify(calls));
      })().catch((error) => {
        console.error(error);
        process.exit(1);
      });
    JS

    stdout, stderr, status = Open3.capture3("node", "-", stdin_data: program)
    abort "#{workflow[:file]} #{result}: #{stderr}" unless status.success?
    calls = JSON.parse(stdout)

    selected = result == "success" ? workflow[:passed] : workflow[:failed]
    opposite = result == "success" ? workflow[:failed] : workflow[:passed]
    remove_index = calls.index { |call| call["op"] == "remove" }
    add_index = calls.index { |call| call["op"] == "add" }
    abort "#{workflow[:file]} #{result}: opposite label not removed" unless calls.dig(remove_index, "args", "name") == opposite
    abort "#{workflow[:file]} #{result}: selected label not added" unless calls.dig(add_index, "args", "labels") == [selected]
    abort "#{workflow[:file]} #{result}: add preceded removal" unless remove_index < add_index

    definitions = calls.select { |call| call["op"] == "update" }.map { |call| call.dig("args", "name") }
    abort "#{workflow[:file]} #{result}: label definitions incomplete" unless definitions.sort == [workflow[:failed], workflow[:passed]].sort
  end
end

puts "4 CI result-label scenarios passed"
```

- [ ] **Step 2: Run the harness and verify RED**

Run:

```bash
ruby /private/tmp/ci-result-labels-test.rb
```

Expected: failure because the workflows do not yet contain a `report` job.

- [ ] **Step 3: Add build reporter permissions and job**

In `.github/workflows/pr-build.yml`, add after the trigger:

```yaml
permissions:
  contents: read
  issues: write
```

Add after the existing `build` job:

```yaml

  report:
    name: Label build result
    if: ${{ always() && !cancelled() }}
    needs: build
    runs-on: ubuntu-24.04
    steps:
      - name: Update build result label
        uses: actions/github-script@v7
        with:
          script: |
            const passed = '${{ needs.build.result }}' === 'success';
            const labels = [
              { name: 'Build Passed', color: '0E8A16', description: 'PR build passed' },
              { name: 'Build Failed', color: 'D93F0B', description: 'PR build failed' },
            ];

            for (const label of labels) {
              try {
                await github.rest.issues.getLabel({ ...context.repo, name: label.name });
                await github.rest.issues.updateLabel({ ...context.repo, ...label });
              } catch (error) {
                if (error.status !== 404) throw error;
                await github.rest.issues.createLabel({ ...context.repo, ...label });
              }
            }

            const selected = passed ? labels[0] : labels[1];
            const opposite = passed ? labels[1] : labels[0];
            try {
              await github.rest.issues.removeLabel({
                ...context.repo,
                issue_number: context.payload.pull_request.number,
                name: opposite.name,
              });
            } catch (error) {
              if (error.status !== 404) throw error;
            }
            await github.rest.issues.addLabels({
              ...context.repo,
              issue_number: context.payload.pull_request.number,
              labels: [selected.name],
            });
            core.info(`Set PR #${context.payload.pull_request.number} to ${selected.name}.`);
```

- [ ] **Step 4: Add MTR reporter permissions and job**

In `.github/workflows/mtr-smoke.yml`, add after the trigger:

```yaml
permissions:
  contents: read
  issues: write
```

Add after the existing `smoke` job:

```yaml

  report:
    name: Label MTR result
    if: ${{ always() && !cancelled() }}
    needs: smoke
    runs-on: ubuntu-24.04
    steps:
      - name: Update MTR result label
        uses: actions/github-script@v7
        with:
          script: |
            const passed = '${{ needs.smoke.result }}' === 'success';
            const labels = [
              { name: 'MTR Passed', color: '0E8A16', description: 'MTR smoke suite passed' },
              { name: 'MTR Failed', color: 'D93F0B', description: 'MTR smoke suite failed' },
            ];

            for (const label of labels) {
              try {
                await github.rest.issues.getLabel({ ...context.repo, name: label.name });
                await github.rest.issues.updateLabel({ ...context.repo, ...label });
              } catch (error) {
                if (error.status !== 404) throw error;
                await github.rest.issues.createLabel({ ...context.repo, ...label });
              }
            }

            const selected = passed ? labels[0] : labels[1];
            const opposite = passed ? labels[1] : labels[0];
            try {
              await github.rest.issues.removeLabel({
                ...context.repo,
                issue_number: context.payload.pull_request.number,
                name: opposite.name,
              });
            } catch (error) {
              if (error.status !== 404) throw error;
            }
            await github.rest.issues.addLabels({
              ...context.repo,
              issue_number: context.payload.pull_request.number,
              labels: [selected.name],
            });
            core.info(`Set PR #${context.payload.pull_request.number} to ${selected.name}.`);
```

- [ ] **Step 5: Run the behavior harness and verify GREEN**

Run:

```bash
ruby /private/tmp/ci-result-labels-test.rb
```

Expected: `4 CI result-label scenarios passed`.

- [ ] **Step 6: Parse YAML and syntax-check both scripts**

Run:

```bash
ruby -e 'require "yaml"; require "open3"; %w[.github/workflows/pr-build.yml .github/workflows/mtr-smoke.yml].each { |file| doc=YAML.load_file(file); script=doc.fetch("jobs").fetch("report").fetch("steps").fetch(0).fetch("with").fetch("script"); _, err, status=Open3.capture3("node", "--check", "-", stdin_data: "async function run() {\n#{script}\n}\n"); abort "#{file}: #{err}" unless status.success? }; puts "workflow yaml and reporter syntax ok"'
```

Expected: `workflow yaml and reporter syntax ok`.

- [ ] **Step 7: Remove the temporary harness**

Delete `/private/tmp/ci-result-labels-test.rb`.

- [ ] **Step 8: Commit the workflow changes**

```bash
git add .github/workflows/pr-build.yml .github/workflows/mtr-smoke.yml
git commit -m "Label build and MTR results"
```

### Task 2: Verify the complete Actions configuration

**Files:**
- Verify: `.github/workflows/*.yml`
- Verify: `.github/workflows/pr-build.yml`
- Verify: `.github/workflows/mtr-smoke.yml`

- [ ] **Step 1: Parse every workflow**

Run:

```bash
ruby -e 'require "yaml"; files=Dir[".github/workflows/*.{yml,yaml}"]; files.each { |file| YAML.load_file(file) }; puts "parsed #{files.length} workflows"'
```

Expected: `parsed 9 workflows`.

- [ ] **Step 2: Verify dependencies, cancellation guards, and labels**

Run:

```bash
rg -n -F 'if: ${{ always() && !cancelled() }}' .github/workflows/pr-build.yml .github/workflows/mtr-smoke.yml
rg -n -F 'needs: build' .github/workflows/pr-build.yml
rg -n -F 'needs: smoke' .github/workflows/mtr-smoke.yml
rg -n -F "name: 'Build Passed'" .github/workflows/pr-build.yml
rg -n -F "name: 'Build Failed'" .github/workflows/pr-build.yml
rg -n -F "name: 'MTR Passed'" .github/workflows/mtr-smoke.yml
rg -n -F "name: 'MTR Failed'" .github/workflows/mtr-smoke.yml
```

Expected: the cancellation guard appears twice and every other search prints one matching line.

- [ ] **Step 3: Inspect the implementation diff and status**

Run:

```bash
git diff --check a9b7decde78..HEAD -- .github/workflows/pr-build.yml .github/workflows/mtr-smoke.yml
git diff --stat a9b7decde78..HEAD -- .github/workflows/pr-build.yml .github/workflows/mtr-smoke.yml
git diff a9b7decde78..HEAD -- .github/workflows/pr-build.yml .github/workflows/mtr-smoke.yml
git status --short
```

Expected: no whitespace errors; the implementation diff contains only the two workflow edits; status contains only the user's pre-existing untracked `.DS_Store` files.
