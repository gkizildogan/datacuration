ARG VLLM_IMAGE=vllm/vllm-openai:v0.19.0
FROM ${VLLM_IMAGE}

# Runtime flags are intentionally supplied by the operator so the exact command
# can be captured in the generation manifest.
ENTRYPOINT ["vllm", "serve"]

