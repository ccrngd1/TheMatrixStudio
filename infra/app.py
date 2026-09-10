#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""CDK app entry point. See infra/README.md for deployment."""

import os

import aws_cdk as cdk

from matrix_infra.config import StackConfig
from matrix_infra.stack import MatrixStudioStack

app = cdk.App()
config = StackConfig.from_context(app.node)

MatrixStudioStack(
    app,
    f"{config.prefix}-stack",
    config=config,
    # Explicit environment rather than environment-agnostic. An agnostic stack
    # cannot use `self.account`/`self.region` in a resource name — it would
    # synthesize a `${AWS::AccountId}` token into the bucket name — and every
    # bucket here is account-scoped so two deployments can share an account.
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=config.region or os.environ.get("CDK_DEFAULT_REGION"),
    ),
    description="TheMatrix Studio: multi-tenant serverless conversation "
    "simulator on Bedrock (Phase 1 skeleton).",
)

app.synth()
