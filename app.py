import streamlit as st
import boto3
import pandas as pd
import concurrent.futures
import inspect
import random
import re
import threading
import time
from botocore.config import Config
from botocore.exceptions import ClientError
from datetime import datetime, timedelta

# ----------------- Config -----------------
st.set_page_config(page_title="EC2 Capacity Block & SageMaker Training Plan Finder", layout="wide")
st.header("🔎 EC2 Capacity Block & SageMaker Training Plan Finder")

# ----------------- Styling -----------------
st.markdown("""
<style>
.stDataFrame table th, .stDataFrame table td {
    text-align: left !important;
}
.stButton > button {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white;
    border: none;
    border-radius: 8px;
    padding: 0.5rem 1rem;
    font-weight: 600;
    box-shadow: 0 4px 12px rgba(102, 126, 234, 0.3);
    transition: all 0.2s ease;
}
.stButton > button:hover {
    transform: translateY(-2px);
    box-shadow: 0 6px 16px rgba(102, 126, 234, 0.4);
}
.stButton > button:active {
    transform: translateY(0px);
    box-shadow: 0 2px 8px rgba(102, 126, 234, 0.3);
}
</style>
""", unsafe_allow_html=True)

# ----------------- Constants -----------------
INSTANCE_TYPES = [
    "p6-b200.48xlarge", "p6-b300.48xlarge",
    "p5.4xlarge","p5.48xlarge","p5e.48xlarge","p5en.48xlarge",
    "p4d.24xlarge","p4de.24xlarge",
    "trn1.32xlarge","trn2.48xlarge", "trn2.3xlarge"
]

AWS_REGIONS = [
    "us-east-1","us-east-2",
    "us-west-1","us-west-2",
    "eu-north-1","eu-west-2","eu-south-2",
    "ap-northeast-1","ap-northeast-2",
    "ap-south-1",
    "ap-southeast-2","ap-southeast-3", "ap-southeast-4",
    "sa-east-1"
]

VALID_DURATIONS = [1,2,3,4,5,6,7,8,9,10,11,12,13,14] + [i for i in range(21,183,7)]

# Human-readable region names (location only, no airport codes)
REGION_LABEL = {
    "us-east-1": "N. Virginia", "us-east-2": "Ohio",
    "us-west-1": "N. California", "us-west-2": "Oregon",
    "eu-north-1": "Stockholm", "eu-west-2": "London", "eu-south-2": "Spain",
    "ap-northeast-1": "Tokyo", "ap-northeast-2": "Seoul",
    "ap-south-1": "Mumbai", "ap-southeast-2": "Sydney",
    "ap-southeast-3": "Jakarta", "ap-southeast-4": "Melbourne",
    "sa-east-1": "São Paulo",
}

SAGEMAKER_TARGET_RESOURCES = {
    "Training Job": "training-job",
    "HyperPod Cluster": "hyperpod-cluster",
    "Endpoint (Inference)": "endpoint",
}

# Max regions scanned concurrently. Instance types are iterated sequentially
# within each region so that only one describe call per region is in flight.
MAX_WORKERS = 8

# Error codes that mean "you are asking too fast". DescribeCapacityBlockOfferings
# enforces a low, slowly-refilling limit per account per region and answers with
# CapacityBlockDescribeLimitExceeded. botocore does not classify that code as
# throttling (it is in neither ThrottledRetryableChecker._THROTTLED_ERROR_CODES
# nor data/_retry.json), so no retry mode -- standard or adaptive -- backs off
# for it. Pacing and retries are handled by RegionLimiter below instead.
THROTTLE_ERROR_CODES = {
    "CapacityBlockDescribeLimitExceeded",
    "RequestLimitExceeded",
    "Throttling",
    "ThrottlingException",
}

# Standard mode still covers genuine 5xx and connection errors. Throttle backoff
# is deliberately left to RegionLimiter so retries are paced per region rather
# than per call.
BOTO_CONFIG = Config(retries={"max_attempts": 3, "mode": "standard"})

# Retry budget for a single throttled call. RETRY_BUDGET_SECONDS is a hard
# ceiling on the total time one call may spend backing off, including time spent
# waiting on the region's token bucket, so a saturated region cannot hold the
# whole scan open. Regions are scanned in parallel, so this is not additive.
RETRY_MAX_ATTEMPTS = 4
RETRY_BASE_SECONDS = 2.0
RETRY_CAP_SECONDS = 8.0
RETRY_BUDGET_SECONDS = 30.0

# ----------------- Helpers -----------------
def log_msg(msg, region=None, instance_type=None):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    func_name = inspect.currentframe().f_back.f_code.co_name
    parts = [timestamp, func_name]
    if region: parts.append(f"region={region}")
    if instance_type: parts.append(f"instance_type={instance_type}")
    print(f"[{' | '.join(parts)}] {msg}")

def parse_iso_date(date_val):
    """Convert AWS string/datetime to datetime"""
    if isinstance(date_val, str):
        if date_val.endswith("Z"):
            return datetime.fromisoformat(date_val.replace("Z", "+00:00"))
        return datetime.fromisoformat(date_val)
    return date_val

def fmt_date(dt):
    """Return timezone-naive datetime for DataFrame display"""
    if dt is None:
        return None
    if hasattr(dt, 'tzinfo') and dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt

_az_zone_id_cache = {}

def get_az_zone_id(az_name):
    """Map AZ name (e.g. us-east-1a) to physical Zone ID (e.g. use1-az1)"""
    if not az_name or az_name == "N/A":
        return "N/A"
    if az_name in _az_zone_id_cache:
        return _az_zone_id_cache[az_name]
    try:
        # Parent region: handles standard AZs (us-east-1a -> us-east-1) AND
        # Local Zones (us-east-1-atl-2a -> us-east-1). Stripping the last char
        # would wrongly give 'us-east-1-atl-2' for Local Zones.
        m = re.match(r"^[a-z]{2}-[a-z]+-\d+", az_name)
        region = m.group(0) if m else az_name[:-1]
        resp = boto3.client("ec2", region_name=region).describe_availability_zones(
            ZoneNames=[az_name], AllAvailabilityZones=True)
        azs = resp.get("AvailabilityZones", [])
        if azs:
            zone_id = azs[0]["ZoneId"]
            if azs[0].get("ZoneType") == "local-zone":
                zone_id += " (Local Zone)"
        else:
            zone_id = "N/A"
        _az_zone_id_cache[az_name] = zone_id
        return zone_id
    except Exception:
        _az_zone_id_cache[az_name] = "N/A"
        return "N/A"

def parse_error(full_error):
    """Turn a raw AWS error string into a short, friendly message"""
    if "CapacityBlockDescribeLimitExceeded" in full_error:
        return "Request rate limit exceeded — results may be incomplete, retry in a minute"
    if any(code in full_error for code in ("RequestLimitExceeded", "ThrottlingException", "Throttling")):
        return "Request rate limit exceeded — retry in a minute"
    if "AuthFailure" in full_error:
        return "Authentication failure — validate credentials and check the region is enabled"
    if "UnknownOperationException" in full_error and "not supported in the called region" in full_error:
        return "Not supported in this region"
    if "ResourceLimitExceeded" in full_error and "instance quota is not sufficient" in full_error:
        match = re.search(r"reserved-capacity ([^\s]+) instance quota", full_error)
        if match:
            return f"'{match.group(1)}' instance quota is not sufficient"
    if "ValidationException" in full_error:
        if "Invalid instance count" in full_error:
            match = re.search(r"Invalid instance count (\d+) for instance type ([^\s]+)", full_error)
            if match:
                return f"Invalid instance count {match.group(1)} for instance type {match.group(2)}"
        if "Invalid instance type" in full_error:
            match = re.search(r"Invalid instance type ([^\s]+)", full_error)
            if match:
                return f"'{match.group(1)}' is not supported in this region"
    if "InvalidParameterValue" in full_error:
        if "start date is not valid" in full_error:
            return "Start date is not valid"
        if "duration is not valid" in full_error:
            return "Invalid duration"
        match = re.search(r"'([^']+)' is not supported", full_error)
        if match:
            return f"'{match.group(1)}' is not supported in this region"
        return "Instance type is not supported in this region"
    return ""

def process_results(results, expected_cols):
    """Split errors from results, sort by start date, and order columns cleanly"""
    if not results:
        return pd.DataFrame(), pd.DataFrame()
    df = pd.DataFrame(results)
    if "Error" in df.columns:
        success_df = df[df["Error"].isna()].drop(columns=["Error"])
        error_df = df[df["Error"].notna()][["Region", "Error"]].rename(columns={"Error": "Full Error"})
        error_df["Error"] = error_df["Full Error"].apply(parse_error)
        error_df = error_df[["Region", "Error", "Full Error"]]
    else:
        success_df, error_df = df, pd.DataFrame()
    if not success_df.empty:
        if "Start Date (UTC)" in success_df.columns:
            success_df = success_df.sort_values("Start Date (UTC)").reset_index(drop=True)
        cols = [c for c in expected_cols if c in success_df.columns]
        success_df = success_df[cols]
    return success_df, error_df

DATE_COL_CONFIG = {
    "Start Date (UTC)": st.column_config.DatetimeColumn(format="DD/MM/YYYY HH:mm"),
    "End Date (UTC)": st.column_config.DatetimeColumn(format="DD/MM/YYYY HH:mm"),
}

RESULT_COLS = [
    "Offering ID", "Region", "Region Name", "Instance Type", "Instance Count",
    "Part", "Duration (days)", "Start Date (UTC)", "End Date (UTC)",
    "Upfront Fee", "Number of Parts", "Availability Zone", "Zone ID"
]

# ----------------- Sidebar Inputs -----------------
st.sidebar.header("Search Parameters")
selected_instance_types = st.sidebar.multiselect("Select Instance Types", INSTANCE_TYPES, default=["p5.48xlarge"])
instance_count = st.sidebar.number_input("Instance Count", min_value=1, max_value=256, value=1)

region_options = ["All Regions"] + AWS_REGIONS
selected_regions = st.sidebar.multiselect(
    "Select Regions", region_options, default=["All Regions"],
    format_func=lambda r: r if r == "All Regions" else f"{REGION_LABEL.get(r, r)} ({r})"
)

duration_days = st.sidebar.selectbox("Duration (days)", VALID_DURATIONS, index=6)
start_date = st.sidebar.date_input("Start Date", datetime.today(), format="DD/MM/YYYY")
use_end_date = st.sidebar.checkbox("Specify End Date", value=False)
end_date = st.sidebar.date_input("End Date", datetime.today() + timedelta(days=14), format="DD/MM/YYYY") if use_end_date else None

selected_target_resource = st.sidebar.selectbox("SageMaker Target Resource", list(SAGEMAKER_TARGET_RESOURCES.keys()))

# ----------------- Validation -----------------
if use_end_date and start_date > end_date:
    st.sidebar.error("Start date must be before end date.")

# ----------------- Rate Limiting -----------------
class RegionLimiter:
    """Token bucket for one service+region pair, with AIMD response to throttling.

    Additive increase / multiplicative decrease: each success nudges the allowed
    request rate up, each throttle halves it and empties the bucket. Draining the
    bucket is what makes every worker touching that region slow down together,
    rather than each one discovering the throttle for itself.
    """

    def __init__(self, rate=0.5, burst=2.0, min_rate=0.2, max_rate=1.0):
        self._rate = rate
        self._burst = burst
        self._min_rate = min_rate
        self._max_rate = max_rate
        self._tokens = burst
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, max_wait=None):
        """Block until a token is available, then consume it.

        Returns True once a token is taken, or False if max_wait ran out first
        (in which case nothing is consumed). The timeout keeps a penalised
        limiter from stalling a Streamlit request indefinitely.
        """
        deadline = None if max_wait is None else time.monotonic() + max_wait
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                wait = (1.0 - self._tokens) / self._rate
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait = min(wait, remaining)
            time.sleep(wait)

    def penalize(self):
        """Halve the rate and drain the bucket after a throttle."""
        with self._lock:
            self._rate = max(self._min_rate, self._rate * 0.5)
            self._tokens = 0.0
            return self._rate

    def reward(self):
        """Creep the rate back up after a success."""
        with self._lock:
            self._rate = min(self._max_rate, self._rate * 1.05)


# Streamlit re-executes this script on every rerun, so module-level state is
# discarded each time. Parking the registry in session_state lets a rate learned
# during one scan carry over to the next click instead of resetting to optimistic.
# The dict is fetched here, on the main thread, and passed to workers by
# reference -- worker threads never touch session_state themselves.
if "rate_limit_registry" not in st.session_state:
    st.session_state["rate_limit_registry"] = {"limiters": {}, "lock": threading.Lock()}
_LIMITERS = st.session_state["rate_limit_registry"]["limiters"]
_LIMITERS_LOCK = st.session_state["rate_limit_registry"]["lock"]

_client_lock = threading.Lock()


def limiter_for(service, region):
    """One limiter per service+region, since the quota is scoped that way."""
    key = (service, region)
    with _LIMITERS_LOCK:
        if key not in _LIMITERS:
            _LIMITERS[key] = RegionLimiter()
        return _LIMITERS[key]


def make_client(service, region):
    """Build a client under a lock.

    boto3 clients are thread-safe once constructed, but constructing them from
    the shared default session concurrently is not.
    """
    with _client_lock:
        return boto3.client(service, region_name=region, config=BOTO_CONFIG)


def is_throttle_error(err):
    """True if an exception or its stringified form is a throttling error."""
    if isinstance(err, ClientError):
        return err.response.get("Error", {}).get("Code", "") in THROTTLE_ERROR_CODES
    return any(code in str(err) for code in THROTTLE_ERROR_CODES)


def throttled_call(service, region, fn, itype=None):
    """Run fn() paced by the region's limiter, retrying throttles with backoff.

    Full jitter (uniform(0, delay)) rather than a fixed sleep: workers tend to
    get throttled at the same moment, and fixed backoff just makes them retry in
    lockstep. Raises the last ClientError if the attempt or time budget runs out.
    """
    limiter = limiter_for(service, region)
    started = time.monotonic()
    last_error = None
    attempt = 0
    while attempt < RETRY_MAX_ATTEMPTS:
        remaining = RETRY_BUDGET_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            log_msg(f"throttle budget of {RETRY_BUDGET_SECONDS:.0f}s spent, giving up", region, itype)
            break
        if not limiter.acquire(max_wait=remaining):
            log_msg("throttle budget spent waiting for a token, giving up", region, itype)
            break
        try:
            result = fn()
            limiter.reward()
            return result
        except ClientError as e:
            if not is_throttle_error(e):
                raise
            last_error = e
            attempt += 1
            rate = limiter.penalize()
            elapsed = time.monotonic() - started
            if attempt >= RETRY_MAX_ATTEMPTS:
                log_msg(f"throttled, giving up after {attempt} attempt(s) in {elapsed:.1f}s", region, itype)
                break
            delay = random.uniform(0, min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * (2 ** (attempt - 1))))
            delay = min(delay, max(0.0, RETRY_BUDGET_SECONDS - elapsed))
            log_msg(
                f"throttled, rate now {rate:.2f}/s, retrying in {delay:.1f}s "
                f"(attempt {attempt}/{RETRY_MAX_ATTEMPTS})",
                region, itype)
            time.sleep(delay)

    if last_error is not None:
        raise last_error
    # Budget went entirely on waiting for a token, so the call never left the
    # process. Report it as a throttle: it is one, just enforced locally after an
    # earlier throttle penalised this region.
    raise ClientError(
        {"Error": {"Code": "CapacityBlockDescribeLimitExceeded",
                   "Message": f"Local rate limiter held back the request to {region} "
                              f"for {RETRY_BUDGET_SECONDS:.0f}s after earlier throttling"}},
        "RateLimited")


def was_throttled(results):
    """True if any raw result row carries a throttling error."""
    return any(
        isinstance(r.get("Error"), str) and is_throttle_error(r["Error"])
        for r in results
    )


# ----------------- AWS EC2 Scan -----------------
def scan_region_instance_type(ec2, region, itype, count, duration):
    try:
        params = {
            "InstanceType": itype,
            "InstanceCount": int(count),
            "CapacityDurationHours": int(duration * 24),
            "StartDateRange": datetime.combine(start_date, datetime.min.time()),
            "AllAvailabilityZones": True,
            "MaxResults": 100
        }
        if use_end_date and end_date:
            params["EndDateRange"] = datetime.combine(end_date, datetime.min.time())
        log_msg(f"EC2 params: {params}", region, itype)

        resp = throttled_call(
            "ec2", region,
            lambda: ec2.describe_capacity_block_offerings(**params),
            itype)
        offerings = resp.get("CapacityBlockOfferings", [])
        log_msg(f"EC2 offerings={len(offerings)}", region, itype)
        results = []
        for o in offerings:
            upfront_fee = f"${o.get('UpfrontFee', '0')}"
            duration_hours = o["CapacityBlockDurationHours"]
            reserved_offerings = o.get("ReservedCapacityOfferings", []) or []
            parts_count = len(reserved_offerings) if reserved_offerings else 1
            offering_id = o.get("CapacityBlockOfferingId", "")[-8:]

            if reserved_offerings and parts_count > 1:
                total_days = duration_hours / 24
                for idx, r in enumerate(reserved_offerings):
                    start_dt = parse_iso_date(r.get("StartDate", o["StartDate"]))
                    end_dt = parse_iso_date(r.get("EndDate", o["EndDate"]))
                    part_duration = r.get("CapacityBlockDurationHours", duration_hours)
                    part_days = part_duration / 24
                    results.append({
                        "Region": region, "Region Name": REGION_LABEL.get(region, ""), "Instance Type": itype,
                        "Instance Count": str(o.get("InstanceCount", 0)),
                        "Offering ID": offering_id,
                        "Part": f"{idx+1} of {parts_count}",
                        "Duration (days)": f"{part_days:.2f} of {total_days:.2f}",
                        "Start Date (UTC)": fmt_date(start_dt),
                        "End Date (UTC)": fmt_date(end_dt),
                        "Upfront Fee": upfront_fee if idx == 0 else "",
                        "Number of Parts": str(parts_count),
                        "Availability Zone": o.get("AvailabilityZone", "N/A"),
                        "Zone ID": get_az_zone_id(o.get("AvailabilityZone", "N/A"))
                    })
            else:
                start_dt, end_dt = parse_iso_date(o["StartDate"]), parse_iso_date(o["EndDate"])
                results.append({
                    "Region": region, "Region Name": REGION_LABEL.get(region, ""), "Instance Type": itype,
                    "Instance Count": str(o.get("InstanceCount", 0)),
                    "Offering ID": offering_id,
                    "Part": "1 of 1",
                    "Duration (days)": f"{duration_hours / 24:.2f}",
                    "Start Date (UTC)": fmt_date(start_dt),
                    "End Date (UTC)": fmt_date(end_dt),
                    "Upfront Fee": upfront_fee,
                    "Number of Parts": str(parts_count),
                    "Availability Zone": o.get("AvailabilityZone", "N/A"),
                    "Zone ID": get_az_zone_id(o.get("AvailabilityZone", "N/A"))
                })
        return results
    except Exception as e:
        log_msg(f"scan_region error: {e}", region, itype)
        return [{"Region": region, "Error": str(e)}]


def scan_region(region, instance_types, count, duration):
    """Scan one region for every requested instance type, one call at a time.

    Instance types are walked sequentially in this thread rather than fanned out,
    so at most one DescribeCapacityBlockOfferings call per region is ever in
    flight. The throttle is scoped per region, and parallelism across regions is
    what actually saves wall-clock time.

    A region that stays throttled after the full retry budget stops here: the
    remaining instance types would each burn their own budget against a quota
    that is clearly exhausted.
    """
    try:
        ec2 = make_client("ec2", region)
    except Exception as e:
        log_msg(f"client creation failed: {e}", region)
        return [{"Region": region, "Error": str(e)}]

    results = []
    for itype in instance_types:
        rows = scan_region_instance_type(ec2, region, itype, count, duration)
        results.extend(rows)
        if was_throttled(rows):
            remaining = instance_types[instance_types.index(itype) + 1:]
            if remaining:
                log_msg(f"skipping {len(remaining)} remaining instance type(s)", region)
            break
    return results

# ----------------- SageMaker Scan -----------------
def scan_sagemaker_instance_type(sm, region, itype, count, duration):
    try:
        params = {
            "TargetResources": [SAGEMAKER_TARGET_RESOURCES[selected_target_resource]],
            "InstanceType": f"ml.{itype}",
            "InstanceCount": int(count),
            "StartTimeAfter": datetime.combine(start_date, datetime.min.time()),
            "DurationHours": int(duration * 24)
        }
        if use_end_date and end_date:
            params["EndTimeBefore"] = datetime.combine(end_date, datetime.min.time())
        log_msg(f"SageMaker params: {params}", region, itype)

        resp = throttled_call(
            "sagemaker", region,
            lambda: sm.search_training_plan_offerings(**params),
            itype)
        offerings = resp.get("TrainingPlanOfferings", [])
        log_msg(f"SageMaker offerings={len(offerings)}", region, itype)
        results = []
        for o in offerings:
            upfront_fee = f"${o.get('UpfrontFee','0')}"
            reserved_offerings = o.get("ReservedCapacityOfferings", [])
            parts_count = len(reserved_offerings)
            duration_hours = o.get("DurationHours", 0)

            if reserved_offerings:
                offering_id = o.get("TrainingPlanOfferingId", "")[-8:]
                total_days = duration_hours / 24
                for idx, r in enumerate(reserved_offerings):
                    start_dt, end_dt = parse_iso_date(r.get("StartTime")), parse_iso_date(r.get("EndTime"))
                    part_duration = r.get("DurationHours", 0) + r.get("DurationMinutes", 0) / 60
                    part_days = part_duration / 24
                    duration_str = f"{part_days:.2f} of {total_days:.2f}" if parts_count > 1 else f"{total_days:.2f}"
                    results.append({
                        "Region": region, "Region Name": REGION_LABEL.get(region, ""),
                        "Instance Type": r.get("InstanceType", itype),
                        "Instance Count": str(r.get("InstanceCount", 0)),
                        "Offering ID": offering_id,
                        "Part": f"{idx+1} of {parts_count}",
                        "Duration (days)": duration_str,
                        "Start Date (UTC)": fmt_date(start_dt),
                        "End Date (UTC)": fmt_date(end_dt),
                        "Upfront Fee": upfront_fee if idx == 0 else "",
                        "Number of Parts": str(parts_count),
                        "Availability Zone": r.get("AvailabilityZone","N/A"),
                        "Zone ID": get_az_zone_id(r.get("AvailabilityZone", "N/A"))
                    })
        return results
    except Exception as e:
        if "InvalidAction" in str(e) or "AuthFailure" in str(e):
            return []
        log_msg(f"scan_sagemaker error: {e}", region, itype)
        return [{"Region": region, "Error": str(e)}]


def scan_sagemaker_region(region, instance_types, count, duration):
    """SageMaker counterpart to scan_region: one region, types walked in order."""
    try:
        sm = make_client("sagemaker", region)
    except Exception as e:
        log_msg(f"client creation failed: {e}", region)
        return [{"Region": region, "Error": str(e)}]

    results = []
    for itype in instance_types:
        rows = scan_sagemaker_instance_type(sm, region, itype, count, duration)
        results.extend(rows)
        if was_throttled(rows):
            remaining = instance_types[instance_types.index(itype) + 1:]
            if remaining:
                log_msg(f"skipping {len(remaining)} remaining instance type(s)", region)
            break
    return results

# ----------------- Run Scans -----------------
col1, col2, col3 = st.columns([1, 1, 4])
with col1: do_capacity = st.button("Find EC2 Capacity Block")
with col2: do_sagemaker = st.button("Find SageMaker Training Plan")

def run_parallel(scan_fn, regions, instance_types, *args):
    """Fan out one task per region.

    Previously this submitted one task per (region, instance_type) pair, which
    let all MAX_WORKERS threads pile onto a single region when several instance
    types were selected -- the fastest way to trip a per-region quota. Each task
    now owns a whole region and iterates instance types internally.
    """
    results = []
    if not regions or not instance_types:
        return results
    workers = min(MAX_WORKERS, len(regions))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(scan_fn, r, list(instance_types), *args) for r in regions]
        for f in concurrent.futures.as_completed(futures):
            results.extend(f.result())
    return results

# EC2 capacity search
if do_capacity:
    scan_regions = AWS_REGIONS if "All Regions" in selected_regions else selected_regions
    with st.spinner(f"Scanning {len(scan_regions)} region(s)..."):
        results = run_parallel(scan_region, scan_regions, selected_instance_types, instance_count, duration_days)
        success, errors = process_results(results, RESULT_COLS)
        throttled = was_throttled(results)
        if success.empty and throttled:
            # Never launch the reduced-parameter pass off the back of a throttle.
            # It used to fire immediately, and against every region rather than
            # the selected ones, which turned one rate limit into a sustained one.
            st.error(
                "🚦 Request rate limit reached before any offerings came back, so the "
                "reduced-parameter retry was skipped to let the limit recover. "
                "Wait a minute and search again, or narrow the regions and instance types."
            )
        elif success.empty:
            st.info("ℹ️ No capacity found. Retrying with reduced params...")
            # Same regions the user selected. The rate limiters carry their learned
            # rates over from the pass above, so this second wave stays paced.
            reduced = run_parallel(scan_region, scan_regions, selected_instance_types, max(1,instance_count//2), max(1,duration_days//2))
            fallback, _ = process_results(reduced, RESULT_COLS)
            if not fallback.empty:
                st.success("✅ Found alternatives with reduced parameters!")
                st.dataframe(fallback, width='stretch', column_config=DATE_COL_CONFIG)
            elif was_throttled(reduced):
                st.error("🚦 Request rate limit reached during the reduced-parameter retry. Wait a minute and try again.")
            else:
                st.warning("⚠️ No offerings found even with reduced parameters.")
        else:
            if throttled:
                st.warning(
                    "🚦 Partial results: some regions hit the request rate limit, so "
                    "offerings there may be missing. Search again in a minute for the full picture."
                )
            else:
                st.success("✅ Capacity blocks found!")
            st.dataframe(success, width='stretch', column_config=DATE_COL_CONFIG)
        if not errors.empty:
            st.warning("⚠️ Some regions returned errors:")
            st.dataframe(errors, width='stretch')

# SageMaker training plan
if do_sagemaker:
    scan_regions = AWS_REGIONS if "All Regions" in selected_regions else selected_regions
    with st.spinner(f"Scanning SageMaker in {len(scan_regions)} region(s)..."):
        results = run_parallel(scan_sagemaker_region, scan_regions, selected_instance_types, instance_count, duration_days)
        success, errors = process_results(results, RESULT_COLS)
        throttled = was_throttled(results)
        if success.empty and throttled:
            st.error("🚦 Request rate limit reached. Wait a minute and search again, or narrow the regions and instance types.")
        elif success.empty:
            st.info("ℹ️ No SageMaker offerings found.")
        else:
            if throttled:
                st.warning("🚦 Partial results: some regions hit the request rate limit, so offerings there may be missing.")
            else:
                st.success("✅ SageMaker offerings found!")
            st.dataframe(success, width='stretch', column_config=DATE_COL_CONFIG)
        if not errors.empty:
            st.warning("⚠️ Some regions returned errors:")
            st.dataframe(errors, width='stretch')
