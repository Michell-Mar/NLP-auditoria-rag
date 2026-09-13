FROM public.ecr.aws/lambda/python:3.12
COPY requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt
COPY app.py worker.py ${LAMBDA_TASK_ROOT}/
COPY static/ ${LAMBDA_TASK_ROOT}/static/
# One image, two Lambda functions: infra/webapp_stack.py overrides CMD per
# function (app.handler for the API, worker.handler for the SQS consumer).
CMD ["app.handler"]
