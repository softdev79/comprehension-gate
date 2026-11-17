# Worker image for AWS Lambda. Build from the project root:
#   docker build -f deploy/Dockerfile -t comprehension-gate-worker .
FROM public.ecr.aws/lambda/python:3.12

# git for cloning pull requests; nothing else is installed system-wide.
RUN dnf install -y git-core && dnf clean all && rm -rf /var/cache/dnf

COPY deploy/requirements-worker.txt /tmp/requirements-worker.txt
RUN pip install --no-cache-dir -r /tmp/requirements-worker.txt

COPY gate/ ${LAMBDA_TASK_ROOT}/gate/
COPY service/ ${LAMBDA_TASK_ROOT}/service/
COPY comprehension_gate.py ${LAMBDA_TASK_ROOT}/

CMD ["service.worker.handler"]
