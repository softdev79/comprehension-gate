"""Build and deploy the stack with SAM, taking model settings from this project's .env.

    python deploy/deploy.py --profile <aws-profile> --app-id <APP_ID> --bot-login "<app-slug>[bot]"

The project ID is passed to CloudFormation as a NoEcho parameter and masked in the output.
The API key is not passed here at all: the worker reads it from SSM.
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gate import llm  # noqa: E402


def sam():
    found = shutil.which("sam") or shutil.which("sam.cmd")
    default = r"C:\Program Files\Amazon\AWSSAMCLI\bin\sam.cmd"
    return found or (default if os.path.exists(default) else "sam")


def run(cmd, secret=""):
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=dict(os.environ, SAM_CLI_TELEMETRY="0", MSYS_NO_PATHCONV="1"))
    out = proc.stdout + proc.stderr
    if secret:
        out = out.replace(secret, "***")
    keep = ("Build Succeeded", "Error", "error:", "FAILED", "_COMPLETE ", "Successfully", "No changes")
    print("\n".join(line for line in out.splitlines() if any(k in line for k in keep)))
    if proc.returncode != 0:
        sys.exit(proc.returncode)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--stack-name", default="comprehension-gate")
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--bot-login", required=True)
    args = parser.parse_args()

    llm.load_dotenv()
    project = os.environ.get("OPENAI_PROJECT_ID", "")
    params = [
        "AppId=" + args.app_id,
        "BotLogin=" + args.bot_login,
        "OpenAIBaseUrl=" + os.environ.get("OPENAI_BASE_URL", "https://bedrock-mantle.us-east-1.api.aws/v1"),
        "OpenAIProjectId=" + project,
        "ModelId=" + llm.openai_model(),
    ]
    run([sam(), "build", "--template", "deploy/template.yaml"])
    run([sam(), "deploy", "--stack-name", args.stack_name, "--profile", args.profile, "--region", args.region,
         "--capabilities", "CAPABILITY_IAM", "--resolve-s3", "--resolve-image-repos",
         "--no-confirm-changeset", "--no-fail-on-empty-changeset",
         "--tags", "project=comprehension-gate", "--parameter-overrides"] + params, secret=project)


if __name__ == "__main__":
    main()
