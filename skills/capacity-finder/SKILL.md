---
name: capacity-finder
description: Find short-term GPU reservations on AWS using EC2 Capacity Blocks and SageMaker Training Plans across multiple regions
allowed-tools:
  - search_ec2_capacity_blocks
  - search_sagemaker_training_plans
  - user_input
---

# GPU Capacity Finder Skill

## Purpose

Help users find available short-term GPU reservations on AWS by searching EC2 Capacity Block offerings and SageMaker Training Plan offerings across multiple regions. This enables teams to identify and reserve compute capacity for ML training, inference, and HPC workloads.

## When to Use This Skill

Use this skill when a user wants to:
- Find available GPU instances for short-term reservation
- Compare EC2 Capacity Block pricing across regions
- Search for SageMaker Training Plan offerings
- Identify the cheapest or earliest available GPU capacity
- Plan ML training jobs that require dedicated compute

## Supported Instance Types

| Instance Type | GPU | Use Case |
|---|---|---|
| p6-b200.48xlarge | NVIDIA B200 | Latest gen training/inference |
| p6-b300.48xlarge | NVIDIA B300 | Latest gen training/inference |
| p5.48xlarge | NVIDIA H100 | Large-scale training |
| p5e.48xlarge | NVIDIA H100 (enhanced) | Enhanced networking |
| p5en.48xlarge | NVIDIA H100 (ENA) | High bandwidth |
| p4d.24xlarge | NVIDIA A100 | Training/inference |
| p4de.24xlarge | NVIDIA A100 (80GB) | Large model training |
| trn1.32xlarge | AWS Trainium | Cost-effective training |
| trn2.48xlarge | AWS Trainium2 | Next-gen training |

## Supported Regions

us-east-1, us-east-2, us-west-1, us-west-2, eu-north-1, eu-west-2, ap-northeast-1, ap-northeast-2, ap-south-1, ap-southeast-2, ap-southeast-3, ap-southeast-4, sa-east-1

## Instructions for Execution

### Step 1: Gather Requirements

Ask the user for their preferences using `user_input`. Key parameters:
- **Instance type(s)**: Which GPU instances? (default: p5.48xlarge)
- **Instance count**: How many instances? (default: 1)
- **Region(s)**: Specific regions or "all"? (default: all)
- **Duration**: How many days? Valid: 1-14 days, or weekly (21, 28, 35... up to 182)
- **Start date**: Earliest acceptable start date (default: today)
- **End date** (optional): Latest acceptable end date
- **Search type**: EC2 Capacity Blocks, SageMaker Training Plans, or both?

### Step 2: Search for Capacity

Based on user requirements:
1. Call `search_ec2_capacity_blocks` for EC2 Capacity Block offerings
2. Call `search_sagemaker_training_plans` for SageMaker Training Plan offerings
3. Both tools search across all specified regions in parallel

### Step 3: Present Results

Format results clearly:
- Sort by **upfront fee** (lowest first) or **start date** (earliest first)
- Show region, instance type, duration, dates, and pricing
- Highlight the best options
- If no results found, suggest:
  - Trying different instance types
  - Expanding to more regions
  - Adjusting duration (shorter often has more availability)
  - Checking back later (capacity is dynamic)

## Best Practices

1. **Start broad**: Search all regions first, then narrow down
2. **Be flexible on dates**: A few days of flexibility often yields more options
3. **Consider alternatives**: If p5 isn't available, p4d or trn1 may work
4. **Check both services**: EC2 Capacity Blocks and SageMaker Training Plans may have different availability
5. **Shorter durations**: 1-7 day blocks tend to have better availability than longer ones
