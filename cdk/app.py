#!/usr/bin/env python3
import aws_cdk as cdk

from stacks.bird_conservation_stack import BirdConservationStack

app = cdk.App()

BirdConservationStack(
    app,
    "BirdConservationStack",
    description="AWS-native bird-call recognition & migration-detection agent",
)

app.synth()
