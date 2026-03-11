import os
import sys

# Add src to python path
sys.path.append(os.getcwd())

from src.config.environment import EnvironmentConfig
from src.domain.user import UserBotConfig, LLMProvider


def main() -> None:
    """List agents and their default model mappings based on domain config."""
    env_config = EnvironmentConfig()
    default_config = UserBotConfig()

    smart_model = default_config.smart_model
    if default_config.smart_llm_provider == LLMProvider.ANTHROPIC:
        if os.getenv("ANTHROPIC_API_KEY"):
            if "gemini" in smart_model:
                smart_model = "claude-sonnet-4-5"
        else:
            smart_model = f"{smart_model} (missing ANTHROPIC_API_KEY)"

    mappings = [
        ("RouterAgent", default_config.light_model),
        ("QuickResponseAgent", default_config.light_model),
        ("SmartResponseAgent", smart_model),
        ("ConsolidationAgent", smart_model),
        ("WebSearchAgent", default_config.light_model),
        ("MemorySearchAgent", "n/a"),
    ]

    print(f"Environment: {env_config.env.value}")
    print(f"Default light_llm_provider: {default_config.light_llm_provider.value}")
    print(f"Default smart_llm_provider: {default_config.smart_llm_provider.value}")
    print("Agent → Default Model")
    print("======================")
    for agent, model_name in mappings:
        print(f"{agent}: {model_name}")


if __name__ == "__main__":
    main()