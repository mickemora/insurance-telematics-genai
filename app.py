#!/usr/bin/env python3
"""CDK entry point for the insurance telematics + gen AI demo."""
import os

import aws_cdk as cdk

from telematics.telematics_stack import TelematicsStack

app = cdk.App()

project = app.node.try_get_context("project") or "insurance-telematics-genai"

TelematicsStack(
    app,
    "InsuranceTelematicsGenAI",
    description="Insurance telematics + gen AI demo (synthetic data only). Safe to destroy.",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION"),
    ),
)

cdk.Tags.of(app).add("project", project)
cdk.Tags.of(app).add("data-classification", "synthetic")

app.synth()
