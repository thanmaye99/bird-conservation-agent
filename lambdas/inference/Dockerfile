# BirdNET inference Lambda packaged as a container image.
# Uses the AWS Lambda Python base image so the runtime interface client is built in.
FROM public.ecr.aws/lambda/python:3.11

# System libs needed by librosa / soundfile for audio decoding.
RUN yum install -y libsndfile && yum clean all

# Install Python deps first for better layer caching.
COPY lambdas/inference/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Warm the BirdNET model into the image so cold starts don't download it.
RUN python -c "from birdnetlib.analyzer import Analyzer; Analyzer()"

# Shared library + handler.
COPY lambdas/shared/ ${LAMBDA_TASK_ROOT}/
COPY lambdas/inference/handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.handler"]
