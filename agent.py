"""AWS GPU Capacity Finder Agent - Strands Agents SDK with Skills

An AI agent that helps you find short-term GPU reservations on AWS
using EC2 Capacity Blocks and SageMaker Training Plans.

Uses Agent Skills for progressive disclosure and a user_input tool
for interactive parameter gathering.
"""

import sys
from pathlib import Path

from strands import Agent
from strands_tools import file_read

from tools import search_ec2_capacity_blocks, search_sagemaker_training_plans, user_input

# Optional: import agentskills if available
try:
    from agentskills import discover_skills, generate_skills_prompt
    HAS_AGENTSKILLS = True
except ImportError:
    HAS_AGENTSKILLS = False


SYSTEM_PROMPT = """You are an AWS GPU Capacity Finder agent. Your job is to help users find
available short-term GPU reservations on AWS using EC2 Capacity Blocks and SageMaker Training Plans.

## Your Workflow

1. **Greet the user** and ask what they're looking for (instance type, count, region, duration)
2. **Use the user_input tool** to ask clarifying questions if needed
3. **Search for capacity** using search_ec2_capacity_blocks and/or search_sagemaker_training_plans
4. **Present results** clearly, sorted by price or availability
5. **Suggest alternatives** if nothing is found

## Key Knowledge

### EC2 Capacity Blocks
- Short-term reserved GPU capacity (1-14 days, or weekly up to 26 weeks)
- Pay upfront for guaranteed capacity
- Available for: p5, p5e, p5en, p4d, p4de, p6-b200, p6-b300, trn1, trn2

### SageMaker Training Plans
- Reserved capacity for SageMaker training jobs
- Same instance types with ml. prefix (e.g., ml.p5.48xlarge)
- May have different availability than EC2 Capacity Blocks

### Tips for Users
- Shorter durations (1-7 days) often have better availability
- us-east-1 and us-west-2 tend to have the most capacity
- If one instance type isn't available, suggest alternatives
- p5.48xlarge (H100) is most popular; trn1 is cost-effective alternative

## Important
- Always ask for requirements before searching
- Search both EC2 and SageMaker unless user specifies one
- Format pricing clearly (these are significant costs)
- If no results, explain why and suggest what to try next
"""


def build_system_prompt() -> str:
    """Build system prompt, optionally with Skills metadata."""
    prompt = SYSTEM_PROMPT

    if HAS_AGENTSKILLS:
        skills_dir = Path(__file__).parent / "skills"
        if skills_dir.exists():
            skills = discover_skills(skills_dir)
            if skills:
                skills_prompt = generate_skills_prompt(skills)
                prompt = f"{prompt}\n\n{skills_prompt}"
                print(f"✓ Loaded {len(skills)} skill(s) via AgentSkills")

    return prompt


def main():
    """Run the GPU Capacity Finder agent interactively."""
    print("=" * 60)
    print("🔎 AWS GPU Capacity Finder Agent")
    print("   Find short-term GPU reservations across AWS regions")
    print("=" * 60)
    print()

    system_prompt = build_system_prompt()

    # Create agent with tools
    agent = Agent(
        system_prompt=system_prompt,
        tools=[
            search_ec2_capacity_blocks,
            search_sagemaker_training_plans,
            user_input,
            file_read,  # For reading SKILL.md if needed
        ],
    )

    # Start the conversation
    print("Starting agent... (type 'quit' to exit)\n")

    # Initial prompt to kick off the interaction
    response = agent(
        "Greet the user and ask them what GPU capacity they're looking for. "
        "Use the user_input tool to gather their requirements."
    )
    print(f"\n{response}")

    # Interactive loop
    while True:
        try:
            user_msg = input("\n👤 You: ").strip()
            if user_msg.lower() in ("quit", "exit", "q"):
                print("\n👋 Goodbye!")
                break
            if not user_msg:
                continue

            response = agent(user_msg)
            print(f"\n🤖 Agent: {response}")

        except KeyboardInterrupt:
            print("\n\n👋 Goodbye!")
            break
        except Exception as e:
            print(f"\n❌ Error: {e}")
            continue


if __name__ == "__main__":
    main()
