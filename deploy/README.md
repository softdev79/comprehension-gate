# Deploy comprehension-gate to AWS (free tier)

Runs the gate as a **GitHub App** instead of GitHub Actions workflows. The gate's code, policy
and state then live outside the pull request branch, so an author cannot edit them.

```
GitHub ──webhook──► Lambda Function URL (webhook, 128 MB)
                      verifies the HMAC signature, drops everything but gate events
                    ─► SQS FIFO (one job at a time per PR, redeliveries de-duplicated) ─► DLQ
                    ─► Lambda container (worker, 1 GB, 15 min)
                         clone PR ─ evidence ─ agent (Bedrock gpt-oss-120b) ─ policy
                         commit questionnaire, comment, set commit statuses
                    ─► DynamoDB (questionnaire hash, grades, sign-off)
Secrets: SSM Parameter Store SecureString (free). Logs: CloudWatch, 14 days.
```

| Service | Use | Free tier |
|---|---|---|
| Lambda + Function URL | webhook, worker | 1M requests + 400k GB-s/month, always free |
| SQS FIFO + DLQ | job queue | 1M requests/month, always free |
| DynamoDB (5 RCU/5 WCU) | gate state | 25 GB + 25 RCU/WCU, always free |
| SSM Parameter Store | 3 secrets | standard parameters are free |
| ECR | worker image, about 226 MB compressed | 500 MB for 12 months, then about $0.02/month |
| CloudWatch | logs, one DLQ alarm | 5 GB logs, 10 alarms free |
| Bedrock gpt-oss-120b | the model | **not free**: pay per token, a few cents per PR |

## You need

- The AWS CLI signed in to the account (`aws login` or `aws configure`), region `us-east-1`.
- AWS SAM CLI. Install it in its own virtual environment, outside this project:
  `python -m venv %USERPROFILE%\sam-venv` then `%USERPROFILE%\sam-venv\Scripts\pip install aws-sam-cli`.
- Docker running (builds the worker image).
- A GitHub repository to test on.

## 1. Create the GitHub App

GitHub → Settings → Developer settings → GitHub Apps → **New GitHub App**:

- **Webhook URL**: `https://example.com` for now (changed in step 4).
- **Webhook secret**: a long random string. Generate one with
  `python -c "import secrets; print(secrets.token_hex(32))"` and keep it for step 2.
- **Repository permissions**: Contents *Read and write*, Pull requests *Read and write*,
  Issues *Read and write* (for comments), Commit statuses *Read and write*, Metadata *Read*.
- **Subscribe to events**: *Pull request*, *Issue comment*.
- **Where can this GitHub App be installed?** Only on this account.

After creating it, note the **App ID** and the **bot login**: `<app-slug>[bot]`, where the slug
is the last part of the App's public URL. Then **Generate a private key** to download a `.pem`.

## 2. Store the secrets in SSM

Read the values from files so they never appear in your shell history. Delete the files afterwards.

```bash
aws ssm put-parameter --name /comprehension-gate/webhook-secret --type SecureString --value file://webhook-secret.txt
```

```bash
aws ssm put-parameter --name /comprehension-gate/github-app-private-key --type SecureString --value file://your-app.private-key.pem
```

```bash
aws ssm put-parameter --name /comprehension-gate/openai-api-key --type SecureString --value file://bedrock-api-key.txt
```

## 3. Build and deploy

From the project root:

```bash
sam build --template deploy/template.yaml
```

```bash
sam deploy --guided --stack-name comprehension-gate --capabilities CAPABILITY_IAM --resolve-image-repos
```

The guided deploy asks for `AppId`, `BotLogin`, `OpenAIProjectId` (and the base URL and model,
whose defaults match the Bedrock endpoint). It saves your answers to `samconfig.toml`; later
deploys are just `sam build --template deploy/template.yaml && sam deploy`.

## 4. Connect GitHub

1. Copy the `WebhookUrl` output into the App's **Webhook URL** and save. GitHub sends a ping,
   which should show a green tick under the App's *Advanced → Recent deliveries*.
2. **Install** the App on the test repository.
3. If the repository has the old workflow files (`comprehension-*.yml`), delete them so the gate
   does not run twice.
4. Branch protection (or a ruleset) on the default branch: require the status check
   `comprehension-gate`, and choose this App as its expected source, so nobody can set it
   with their own token.

## 5. Try it

Open a pull request that changes code. Within a minute or two the App commits
`.comprehension/pr-<n>/answers.md` to the branch and comments. Fill in the answers, push, read
the grade comment, then comment `/sign`.

Logs: `sam logs --stack-name comprehension-gate --name WorkerFunction --tail`.

## 6. Set a cost alarm

```bash
aws budgets create-budget --account-id <ACCOUNT_ID> --budget "{\"BudgetName\":\"comprehension-gate\",\"BudgetLimit\":{\"Amount\":\"5\",\"Unit\":\"USD\"},\"TimeUnit\":\"MONTHLY\",\"BudgetType\":\"COST\"}" --notifications-with-subscribers "[{\"Notification\":{\"NotificationType\":\"ACTUAL\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":80},\"Subscribers\":[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"<YOUR_EMAIL>\"}]}]"
```

## Differences from the Actions version

- The questionnaire hash is checked against DynamoDB, not a bot comment.
- `comprehension-gate.json` is read from the **base** branch, so a PR cannot loosen its own policy.
- `/sign` records the sign-off in DynamoDB and sets the status. It does **not** yet create a
  Sigstore attestation (that used GitHub's OIDC token inside Actions).
- Fork pull requests are still skipped.

## Remove it

```bash
sam delete --stack-name comprehension-gate
```

Then delete the three SSM parameters and uninstall the App.
