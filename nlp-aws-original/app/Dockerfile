FROM public.ecr.aws/lambda/python:3.12
COPY requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt
COPY audito_rag_pdf.py worker.py ${LAMBDA_TASK_ROOT}/
CMD ["worker.handler"]
