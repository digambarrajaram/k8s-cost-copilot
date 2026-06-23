import boto3
import os


def get_bedrock_client(region_name="us-east-1"):
    """Create a bedrock-runtime client.

    Uses explicit env vars when present (AWS_ACCESS_KEY_ID /
    AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN), otherwise falls back to
    boto3's default credential chain: env -> shared-credentials ->
    instance-profile (IMDS/EC2).

    Also strips Bedrock API-key env vars so that ChatBedrockConverse
    does NOT attempt bearer-token auth (which would shadow the EC2
    instance profile and fail with NoAuthTokenError when the token is
    expired or invalid).
    """
    # Strip any lingering API-key env vars so the EC2 instance profile
    # is used instead of broken bearer-token auth.
    for key in ("AWS_BEARER_TOKEN_BEDROCK", "BEDROCK_API_KEY", "AWS_API_KEY"):
        os.environ.pop(key, None)

    creds = {}
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        creds["aws_access_key_id"] = os.environ["AWS_ACCESS_KEY_ID"]
        creds["aws_secret_access_key"] = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        session_token = os.environ.get("AWS_SESSION_TOKEN")
        if session_token:
            creds["aws_session_token"] = session_token

    session = boto3.Session(region_name=region_name, **creds)
    return session.client("bedrock-runtime")
