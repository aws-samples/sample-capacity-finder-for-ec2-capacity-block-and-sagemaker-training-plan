"""Custom tools for AWS GPU capacity search.

Provides search_ec2_capacity_blocks and search_sagemaker_training_plans tools
for the Strands agent to find available short-term GPU reservations.
"""

import boto3
import concurrent.futures
from datetime import datetime, timedelta
from strands import tool

# Constants
INSTANCE_TYPES = [
    "p6-b200.48xlarge", "p6-b300.48xlarge",
    "p5.4xlarge", "p5.48xlarge", "p5e.48xlarge", "p5en.48xlarge",
    "p4d.24xlarge", "p4de.24xlarge",
    "trn1.32xlarge", "trn2.48xlarge", "trn2.3xlarge",
]

AWS_REGIONS = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-north-1", "eu-west-2",
    "ap-northeast-1", "ap-northeast-2", "ap-south-1",
    "ap-southeast-2", "ap-southeast-3", "ap-southeast-4",
    "sa-east-1",
]

MAX_WORKERS = 8


def _parse_date(date_str: str) -> datetime:
    """Parse date string (YYYY-MM-DD) to datetime."""
    return datetime.strptime(date_str, "%Y-%m-%d")


def _scan_ec2_region(region: str, instance_type: str, count: int, duration_days: int,
                     start_date: datetime, end_date: datetime | None) -> list[dict]:
    """Scan a single region for EC2 Capacity Block offerings."""
    try:
        ec2 = boto3.client("ec2", region_name=region)
        params = {
            "InstanceType": instance_type,
            "InstanceCount": count,
            "CapacityDurationHours": duration_days * 24,
            "StartDateRange": start_date,
            "MaxResults": 100,
        }
        if end_date:
            params["EndDateRange"] = end_date

        resp = ec2.describe_capacity_block_offerings(**params)
        offerings = resp.get("CapacityBlockOfferings", [])

        results = []
        for o in offerings:
            start_dt = o["StartDate"] if isinstance(o["StartDate"], datetime) else datetime.fromisoformat(str(o["StartDate"]))
            end_dt = o["EndDate"] if isinstance(o["EndDate"], datetime) else datetime.fromisoformat(str(o["EndDate"]))
            results.append({
                "region": region,
                "instance_type": instance_type,
                "instance_count": o.get("InstanceCount", count),
                "duration_days": round(o["CapacityBlockDurationHours"] / 24, 1),
                "start_date": start_dt.strftime("%Y-%m-%d %H:%M UTC"),
                "end_date": end_dt.strftime("%Y-%m-%d %H:%M UTC"),
                "upfront_fee": f"${o.get('UpfrontFee', '0')}",
                "availability_zone": o.get("AvailabilityZone", "N/A"),
            })
        return results
    except Exception as e:
        error_msg = str(e)
        # Skip common non-errors (unsupported instance type in region, etc.)
        if "InvalidParameterValue" in error_msg or "Unsupported" in error_msg:
            return []
        return [{"region": region, "error": error_msg}]


def _scan_sagemaker_region(region: str, instance_type: str, count: int,
                           duration_days: int, start_date: datetime,
                           end_date: datetime | None) -> list[dict]:
    """Scan a single region for SageMaker Training Plan offerings."""
    try:
        sm = boto3.client("sagemaker", region_name=region)
        params = {
            "TargetResources": ["training-job"],
            "InstanceType": f"ml.{instance_type}",
            "InstanceCount": count,
            "StartTimeAfter": start_date,
            "DurationHours": duration_days * 24,
        }
        if end_date:
            params["EndTimeBefore"] = end_date

        resp = sm.search_training_plan_offerings(**params)
        offerings = resp.get("TrainingPlanOfferings", [])

        results = []
        for o in offerings:
            reserved = o.get("ReservedCapacityOfferings", [])
            if reserved:
                r = reserved[0]
                start_dt = r.get("StartTime")
                end_dt = r.get("EndTime")
                if isinstance(start_dt, str):
                    start_dt = datetime.fromisoformat(start_dt)
                if isinstance(end_dt, str):
                    end_dt = datetime.fromisoformat(end_dt)

                results.append({
                    "region": region,
                    "instance_type": r.get("InstanceType", f"ml.{instance_type}"),
                    "instance_count": r.get("InstanceCount", count),
                    "duration_days": round(o.get("DurationHours", 0) / 24, 1),
                    "start_date": start_dt.strftime("%Y-%m-%d %H:%M UTC") if start_dt else "N/A",
                    "end_date": end_dt.strftime("%Y-%m-%d %H:%M UTC") if end_dt else "N/A",
                    "upfront_fee": f"${o.get('UpfrontFee', '0')}",
                    "availability_zone": r.get("AvailabilityZone", "N/A"),
                    "parts": len(reserved),
                })
        return results
    except Exception as e:
        error_msg = str(e)
        if "InvalidAction" in error_msg or "AuthFailure" in error_msg:
            return []
        if "InvalidParameterValue" in error_msg or "Unsupported" in error_msg:
            return []
        return [{"region": region, "error": error_msg}]


@tool
def search_ec2_capacity_blocks(
    instance_types: list[str] = ["p5.48xlarge"],
    instance_count: int = 1,
    regions: list[str] = ["all"],
    duration_days: int = 7,
    start_date: str = "",
    end_date: str = "",
) -> dict:
    """Search for EC2 Capacity Block offerings across AWS regions.

    Finds available short-term GPU reservations with guaranteed capacity.

    Args:
        instance_types: List of EC2 instance types to search (e.g., ["p5.48xlarge", "p4d.24xlarge"])
        instance_count: Number of instances needed (1-256)
        regions: List of AWS regions to search, or ["all"] for all supported regions
        duration_days: Reservation duration in days (1-14, or weekly: 21, 28, ... 182)
        start_date: Earliest start date (YYYY-MM-DD format, default: today)
        end_date: Latest end date (YYYY-MM-DD format, optional)

    Returns:
        Dictionary with 'offerings' list and 'errors' list
    """
    # Resolve regions
    search_regions = AWS_REGIONS if "all" in regions else [r for r in regions if r in AWS_REGIONS]

    # Resolve dates
    start_dt = _parse_date(start_date) if start_date else datetime.now()
    end_dt = _parse_date(end_date) if end_date else None

    # Validate instance types
    valid_types = [t for t in instance_types if t in INSTANCE_TYPES]
    if not valid_types:
        return {
            "offerings": [],
            "errors": [f"No valid instance types. Supported: {', '.join(INSTANCE_TYPES)}"],
            "summary": "No valid instance types provided.",
        }

    # Parallel scan
    all_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(_scan_ec2_region, region, itype, instance_count, duration_days, start_dt, end_dt)
            for region in search_regions
            for itype in valid_types
        ]
        for future in concurrent.futures.as_completed(futures):
            all_results.extend(future.result())

    # Separate successes and errors
    offerings = [r for r in all_results if "error" not in r]
    errors = [r for r in all_results if "error" in r]

    # Sort by upfront fee
    offerings.sort(key=lambda x: float(x["upfront_fee"].replace("$", "").replace(",", "") or "0"))

    summary = f"Found {len(offerings)} EC2 Capacity Block offering(s) across {len(search_regions)} region(s) for {', '.join(valid_types)}."
    if not offerings:
        summary += " No capacity currently available. Try different instance types, regions, or shorter duration."

    return {
        "offerings": offerings,
        "errors": [e["error"] for e in errors] if errors else [],
        "summary": summary,
        "search_params": {
            "instance_types": valid_types,
            "instance_count": instance_count,
            "regions": search_regions,
            "duration_days": duration_days,
            "start_date": start_dt.strftime("%Y-%m-%d"),
            "end_date": end_dt.strftime("%Y-%m-%d") if end_dt else None,
        },
    }


@tool
def search_sagemaker_training_plans(
    instance_types: list[str] = ["p5.48xlarge"],
    instance_count: int = 1,
    regions: list[str] = ["all"],
    duration_days: int = 7,
    start_date: str = "",
    end_date: str = "",
) -> dict:
    """Search for SageMaker Training Plan offerings across AWS regions.

    Finds available reserved capacity for SageMaker training jobs.

    Args:
        instance_types: List of instance types to search (without ml. prefix, e.g., ["p5.48xlarge"])
        instance_count: Number of instances needed (1-256)
        regions: List of AWS regions to search, or ["all"] for all supported regions
        duration_days: Reservation duration in days (1-14, or weekly: 21, 28, ... 182)
        start_date: Earliest start date (YYYY-MM-DD format, default: today)
        end_date: Latest end date (YYYY-MM-DD format, optional)

    Returns:
        Dictionary with 'offerings' list and 'errors' list
    """
    search_regions = AWS_REGIONS if "all" in regions else [r for r in regions if r in AWS_REGIONS]

    start_dt = _parse_date(start_date) if start_date else datetime.now()
    end_dt = _parse_date(end_date) if end_date else None

    valid_types = [t for t in instance_types if t in INSTANCE_TYPES]
    if not valid_types:
        return {
            "offerings": [],
            "errors": [f"No valid instance types. Supported: {', '.join(INSTANCE_TYPES)}"],
            "summary": "No valid instance types provided.",
        }

    all_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(_scan_sagemaker_region, region, itype, instance_count, duration_days, start_dt, end_dt)
            for region in search_regions
            for itype in valid_types
        ]
        for future in concurrent.futures.as_completed(futures):
            all_results.extend(future.result())

    offerings = [r for r in all_results if "error" not in r]
    errors = [r for r in all_results if "error" in r]

    offerings.sort(key=lambda x: float(x["upfront_fee"].replace("$", "").replace(",", "") or "0"))

    summary = f"Found {len(offerings)} SageMaker Training Plan offering(s) across {len(search_regions)} region(s) for {', '.join(valid_types)}."
    if not offerings:
        summary += " No capacity currently available. Try different instance types, regions, or shorter duration."

    return {
        "offerings": offerings,
        "errors": [e["error"] for e in errors] if errors else [],
        "summary": summary,
        "search_params": {
            "instance_types": [f"ml.{t}" for t in valid_types],
            "instance_count": instance_count,
            "regions": search_regions,
            "duration_days": duration_days,
            "start_date": start_dt.strftime("%Y-%m-%d"),
            "end_date": end_dt.strftime("%Y-%m-%d") if end_dt else None,
        },
    }


@tool
def user_input(question: str, options: list[str] | None = None) -> str:
    """Ask the user a question and wait for their response.

    Use this tool when you need to gather information from the user,
    such as their preferences for instance types, regions, or duration.

    Args:
        question: The question to ask the user
        options: Optional list of suggested options to present

    Returns:
        The user's response as a string
    """
    print(f"\n{'='*60}")
    print(f"🤖 Agent Question: {question}")
    if options:
        print("\nSuggested options:")
        for i, opt in enumerate(options, 1):
            print(f"  {i}. {opt}")
        print(f"  (or type your own answer)")
    print(f"{'='*60}")

    response = input("\n👤 Your answer: ").strip()

    # If user entered a number and options exist, map to option
    if options and response.isdigit():
        idx = int(response) - 1
        if 0 <= idx < len(options):
            response = options[idx]

    return response
