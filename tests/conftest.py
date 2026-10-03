"""Pytest fixtures and configuration."""

import os
import pytest

# Pre-set test environment variables before tests run
os.environ["OPENAI_API_KEY"] = "sk-test-mock-key"
os.environ["CHECKPOINT_BACKEND"] = "memory"
os.environ["WORKSPACE_ROOT"] = "./data/test_workspace"
os.environ["HITL_ENABLED"] = "true"
