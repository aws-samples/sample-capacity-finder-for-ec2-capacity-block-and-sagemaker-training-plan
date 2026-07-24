import streamlit as st
import boto3
import pandas as pd
import altair as alt
from datetime import datetime, timedelta, timezone
import concurrent.futures
import inspect
import time

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
    "eu-north-1","eu-west-2",
    "ap-northeast-1","ap-northeast-2",
    "ap-south-1",
    "ap-southeast-2","ap-southeast-3", "ap-southeast-4",
    "sa-east-1"
]

VALID_DURATIONS = [1,2,3,4,5,6,7,8,9,10,11,12,13,14] + [i for i in range(21,183,7)]

MAX_WORKERS = 8
TIMELINE_HORIZON_DAYS = 56
MAX_TIMELINE_COMBOS = 6
MAX_COUNT = 64

# DescribeCapacityBlockOfferings has its own tiny token bucket, per account per
# region: 10 burst, 0.15 tokens/sec refill (docs: EC2 API request throttling).
CB_BUCKET_CAPACITY = 10
CB_REFILL_RATE = 0.15

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

def process_results(results, expected_cols):
    """Split errors from results and order columns cleanly"""
    if not results:
        return pd.DataFrame(), pd.DataFrame()
    df = pd.DataFrame(results)
    if "Error" in df.columns:
        success_df = df[df["Error"].isna()].drop(columns=["Error"])
        error_df = df[df["Error"].notna()][["Region", "Error"]]
    else:
        success_df, error_df = df, pd.DataFrame()
    if not success_df.empty:
        cols = [c for c in expected_cols if c in success_df.columns]
        success_df = success_df[cols]
    return success_df, error_df

# ----------------- Timeline Probing -----------------
# Duration-ladder probing: one offering of duration D at count N starting day S
# proves count >= N on every day in [S, S+D) — a single long-duration call can
# confirm the whole window. Combined with hop semantics (a StartDateRange query
# returns the EARLIEST offering >= cursor, so a hit at S also confirms nothing
# starts in [cursor, S)), the full per-day staircase costs ~15-25 calls instead
# of 1-2 calls per day.

@st.cache_resource
def _cb_store():
    """Query cache and pacer state. Streamlit re-executes this whole script on
    every interaction, so these must live behind cache_resource to survive
    reruns — a fresh pacer would burst 10 unpaced calls into a possibly-drained
    server-side bucket. Process scope (shared across browser sessions) is
    correct here because the real quota is per-account-per-region."""
    return {"memo": {}, "pacer": {}}

_cb_memo = _cb_store()["memo"]    # (region, itype, count, duration_h, cursor_date) -> offerings
_cb_pacer = _cb_store()["pacer"]  # region -> {"tokens": float, "last": monotonic seconds}

class CbRateLimited(Exception):
    pass

def _pace(region):
    """Client-side token bucket mirroring the real API quota so we never trip it.
    NOT thread-safe (unlocked read-modify-write): timeline combos run
    sequentially today — add a lock before parallelizing scans."""
    bucket = _cb_pacer.setdefault(region, {"tokens": float(CB_BUCKET_CAPACITY),
                                           "last": time.monotonic()})
    now = time.monotonic()
    bucket["tokens"] = min(CB_BUCKET_CAPACITY,
                           bucket["tokens"] + (now - bucket["last"]) * CB_REFILL_RATE)
    bucket["last"] = now
    if bucket["tokens"] < 1.0:
        wait = (1.0 - bucket["tokens"]) / CB_REFILL_RATE
        time.sleep(wait)
        bucket["tokens"] = 1.0
        bucket["last"] = time.monotonic()
    bucket["tokens"] -= 1.0

def cb_query(region, itype, count, duration_hours, cursor=None):
    """Single choke point for all timeline probes: memo cache -> pacer -> API.
    Returns the offerings list. Raises CbRateLimited if throttled despite pacing."""
    key = (region, itype, count, duration_hours,
           cursor.date() if cursor else None)
    if key in _cb_memo:
        return _cb_memo[key]

    ec2 = boto3.client("ec2", region_name=region)
    params = {"InstanceType": itype, "InstanceCount": count,
              "CapacityDurationHours": duration_hours, "MaxResults": 100}
    if cursor is not None:
        params["StartDateRange"] = cursor

    for attempt in range(3):
        _pace(region)
        try:
            resp = ec2.describe_capacity_block_offerings(**params)
            offs = resp.get("CapacityBlockOfferings", [])
            _cb_memo[key] = offs
            log_msg(f"live call: count={count} dur={duration_hours}h "
                    f"cursor={key[4]} -> {len(offs)} offerings", region, itype)
            return offs
        except Exception as e:
            err = str(e)
            if ("RequestLimitExceeded" in err or "PendingVerification" in err
                    or "describe limit" in err.lower()):
                if attempt < 2:
                    log_msg(f"throttled despite pacing, waiting 70s (attempt {attempt+1})", region, itype)
                    time.sleep(70)  # full bucket refill
                    continue
                raise CbRateLimited()
            raise

def longest_valid_duration(days_remaining):
    """Largest purchasable duration (days) that fits in the remaining window."""
    fits = [d for d in VALID_DURATIONS if d <= days_remaining]
    return fits[-1] if fits else None

def _day_start(day):
    return datetime.combine(day, datetime.min.time()).replace(tzinfo=timezone.utc)

def find_coverage(region, itype, window_start, window_end, available):
    """Phase 1: which days have >= 1 instance. Paint with the longest valid
    duration first (one hit covers its whole span), then enumerate the head gap
    with 1-day hops. A long-duration MISS proves nothing per-day, so misses fall
    down the ladder. Writes dates into the caller's `available` set as they are
    proven (so progress survives a mid-scan failure); returns band_start or None."""
    band_start = None
    cursor = _day_start(window_start)
    end_dt = _day_start(window_end)

    ladder = [d for d in (56, 28, 14, 7, 1) if d in VALID_DURATIONS]
    # Rungs that can never hit again: "duration is not valid" is a property of
    # the pool, and an empty result from cursor X stays empty from any later
    # cursor (offerings >= later cursor are a subset). Without this, a pool
    # with only short blocks costs ~5 calls per painted day.
    dead = set()
    while cursor < end_dt:
        days_left = (end_dt - cursor).days
        painted = False
        for dur in ladder:
            if dur in dead or (dur > days_left and dur != 1):
                continue
            try:
                offs = cb_query(region, itype, 1, dur * 24, cursor)
            except CbRateLimited:
                raise
            except Exception as e:
                err = str(e)
                if "duration is not valid" in err.lower():
                    dead.add(dur)
                    continue  # this pool doesn't sell this duration; step down
                if "InvalidParameterValue" in err and "date" in err.lower():
                    return band_start  # past the searchable window
                raise
            if not offs:
                dead.add(dur)
                continue  # no D-day continuous run anywhere ahead; step down
            start = min(o["StartDate"] for o in offs).date()
            if start >= window_end:
                return band_start
            span_end = min(start + timedelta(days=dur), window_end)
            d = start
            while d < span_end:
                available.add(d)
                d += timedelta(days=1)
            if band_start is None and dur > 1:
                band_start = start
            # head gap [cursor, start): only 1-day offerings can live there
            if dur > 1 and start > cursor.date():
                gap_cur = cursor
                while gap_cur.date() < start:
                    gap_offs = cb_query(region, itype, 1, 24, gap_cur)
                    if not gap_offs:
                        break
                    gd = min(o["StartDate"] for o in gap_offs).date()
                    if gd >= start:
                        break
                    available.add(gd)
                    gap_cur = _day_start(gd + timedelta(days=1))
            cursor = _day_start(span_end)
            painted = True
            break
        if not painted:
            break  # even 1-day probe found nothing ahead: rest of window empty
    return band_start

def _has_count_on(region, itype, day, count, duration_h=24):
    offs = cb_query(region, itype, count, duration_h, _day_start(day))
    return any(o["StartDate"].date() == day for o in offs)

def probe_count_with_hint(region, itype, target_date, hint):
    """Find max purchasable count on a date via gallop + binary search,
    starting from a hint (e.g. the previous plateau's count)."""
    has = lambda c: _has_count_on(region, itype, target_date, c)
    hint = max(1, min(hint, MAX_COUNT))
    if has(hint):
        if hint == MAX_COUNT or not has(hint + 1):
            return hint
        lo, step = hint + 1, 2
        while lo + step <= MAX_COUNT and has(lo + step):
            lo += step
            step *= 2
        hi = min(lo + step - 1, MAX_COUNT)
    else:
        if hint == 1:
            return 0  # sold out since discovery
        lo, hi = 1, hint - 1
        if not has(lo):
            return 0
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if has(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo

def find_staircase(region, itype, band_start, window_end, counts, progress_cb=None):
    """Phase 2: exact max count per day across the continuous band.
    The step search hops upward (count+1) assuming capacity ramps up, but the
    result does NOT rely on that assumption: every plateau is then proven by
    long-duration probes pinned at each day range. If capacity goes up then
    down, the pin fails (a long block needs the count continuously), and the
    dipped days are probed individually instead. Writes date -> count into the
    caller's `counts` dict as days are proven, so progress survives a mid-scan
    rate limit or error."""
    step_starts = []   # [(day, count)]
    day = band_start
    c = probe_count_with_hint(region, itype, band_start, 1)
    step_starts.append((band_start, c))
    while c < MAX_COUNT:
        offs = cb_query(region, itype, c + 1, 24, _day_start(day))
        nxt = [o["StartDate"].date() for o in offs if o["StartDate"].date() < window_end]
        if not nxt:
            break  # no higher step anywhere in the window
        day = min(nxt)
        c = probe_count_with_hint(region, itype, day, c + 1)
        step_starts.append((day, c))
        if progress_cb:
            progress_cb(min(0.9, 0.5 + 0.1 * len(step_starts)))

    # Prove every day of every plateau. The step search's count+1 hops already
    # give the upper bound (hop semantics: the earliest day with count+1 is the
    # next step's start, so no earlier day exceeds count). The lower bound is
    # proven by probes pinned at the cursor, longest duration first: a pinned
    # hit covers `dur` days, so advance and repeat. A long pin failing while a
    # shorter one succeeds means the count holds now but dips later; if even
    # the 1-day pin fails, the count dipped AT the cursor — those days are
    # probed individually until the 1-day probe says the count resumes.
    boundaries = step_starts + [(window_end, None)]
    for (start, count), (nxt_start, _) in zip(boundaries, boundaries[1:]):
        cursor = start
        while count > 0 and cursor < nxt_start:
            span_days = (nxt_start - cursor).days
            actual = []
            pinned = 0
            for dur in [d for d in (56, 28, 14, 7, 3, 1) if d <= span_days]:
                offs = cb_query(region, itype, count, dur * 24, _day_start(cursor))
                actual = [o["StartDate"].date() for o in offs]
                if actual and min(actual) == cursor:
                    pinned = dur
                    break
            if pinned:
                d = cursor
                while d < cursor + timedelta(days=pinned):
                    counts[d] = count
                    d += timedelta(days=1)
                cursor += timedelta(days=pinned)
            else:
                # dip at the cursor; `actual` (from the 1-day probe) says when
                # this count next resumes, if ever within the plateau
                resume = min([a for a in actual if a < nxt_start] or [nxt_start])
                d = cursor
                while d < resume:
                    counts[d] = probe_count_with_hint(region, itype, d, count)
                    d += timedelta(days=1)
                cursor = resume
        # count == 0 plateaus (sold out since discovery) fall through as zeros
        d = cursor
        while d < nxt_start:
            counts.setdefault(d, count)
            d += timedelta(days=1)

def scan_timeline(region, itype, progress_cb=None):
    """Timeline scan via the duration ladder: coverage painting first (~2-5
    calls), then exact per-day counts as a verified staircase (~10-20 calls)."""
    now_date = datetime.now(timezone.utc).date()
    window_end = now_date + timedelta(days=TIMELINE_HORIZON_DAYS)
    rate_limited = False
    scan_error = None
    counts = {}
    available = set()

    try:
        band_start = find_coverage(region, itype, now_date, window_end, available)
        if progress_cb:
            progress_cb(0.3)

        if not available:
            return None, "No capacity found in the searchable window"

        # isolated head-gap days (before the continuous band): exact count per day
        isolated = sorted(d for d in available if band_start is None or d < band_start)
        hint = 1
        for d in isolated:
            counts[d] = probe_count_with_hint(region, itype, d, hint)
            hint = max(counts[d], 1)
        if progress_cb:
            progress_cb(0.5)

        if band_start is not None:
            find_staircase(region, itype, band_start, window_end, counts,
                           progress_cb=progress_cb)
    except CbRateLimited:
        rate_limited = True
        if not available:
            return None, "API describe limit reached — results are incomplete. Try again later."
        for d in available:
            counts.setdefault(d, 1)  # confirmed available, exact count unknown
    except Exception as e:
        if not available:
            return None, str(e)[:120]
        scan_error = str(e)[:120]
        for d in available:
            counts.setdefault(d, 1)  # confirmed available, exact count unknown

    if progress_cb:
        progress_cb(0.95)

    all_days = []
    for i in range(TIMELINE_HORIZON_DAYS):
        day = now_date + timedelta(days=i)
        all_days.append({
            "Date": day,
            "Max Instances": counts.get(day, 0),
            "Available": counts.get(day, 0) > 0,
        })

    df = pd.DataFrame(all_days)
    df["Date"] = pd.to_datetime(df["Date"])
    warning = None
    if rate_limited:
        warning = "⚠️ API describe limit reached mid-scan — some counts are estimates. Try again later for full results."
    elif scan_error:
        warning = f"⚠️ Scan interrupted by an error — some counts are estimates. ({scan_error})"
    return df, warning

# ----------------- Sidebar Inputs -----------------
st.sidebar.header("Search Parameters")
selected_instance_types = st.sidebar.multiselect("Select Instance Types", INSTANCE_TYPES, default=["p5.48xlarge"])
instance_count = st.sidebar.number_input("Instance Count", min_value=1, max_value=256, value=1)

region_options = ["All Regions"] + AWS_REGIONS
selected_regions = st.sidebar.multiselect("Select Regions", region_options, default=["All Regions"])

duration_days = st.sidebar.selectbox("Duration (days)", VALID_DURATIONS, index=6)
start_date = st.sidebar.date_input("Start Date", datetime.today(), format="DD/MM/YYYY")
use_end_date = st.sidebar.checkbox("Specify End Date", value=False)
end_date = st.sidebar.date_input("End Date", datetime.today() + timedelta(days=14), format="DD/MM/YYYY") if use_end_date else None

# ----------------- Validation -----------------
if use_end_date and start_date > end_date:
    st.sidebar.error("Start date must be before end date.")

# ----------------- AWS EC2 Scan -----------------
def scan_region(region, itype, count, duration, fallback=False):
    try:
        ec2 = boto3.client("ec2", region_name=region)
        params = {
            "InstanceType": itype,
            "InstanceCount": int(count),
            "CapacityDurationHours": int(duration * 24),
            "StartDateRange": datetime.combine(start_date, datetime.min.time()),
            "MaxResults": 100
        }
        if use_end_date and end_date:
            params["EndDateRange"] = datetime.combine(end_date, datetime.min.time())
        log_msg(f"EC2 params: {params}", region, itype)

        resp = ec2.describe_capacity_block_offerings(**params)
        log_msg(f"EC2 API Response: {resp}", region, itype)
        offerings = resp.get("CapacityBlockOfferings", [])
        results = []
        for o in offerings:
            start_dt, end_dt = parse_iso_date(o["StartDate"]), parse_iso_date(o["EndDate"])
            upfront_fee = f"${o.get('UpfrontFee', '0')}"
            duration_hours = o["CapacityBlockDurationHours"]
            reserved_offerings = o.get("ReservedCapacityOfferings", [{}]) or [{}]
            parts_count = len(reserved_offerings)
            
            results.append({
                "Region": region, "Instance Type": itype,
                "Instance Count": str(o.get("InstanceCount", 0)),
                "Duration (days)": f"{duration_hours / 24:.2f}",
                "Start Date": start_dt.strftime("%d/%m/%Y %H:%M"),
                "End Date": end_dt.strftime("%d/%m/%Y %H:%M"),
                "Upfront Fee": upfront_fee,
                "Number of Parts": str(parts_count),
                "Availability Zone": o.get("AvailabilityZone", "N/A")
            })
        return results
    except Exception as e:
        return [{"Region": region, "Error": str(e)}]

# ----------------- SageMaker Scan -----------------
def scan_sagemaker_region(region, itype, count, duration):
    try:
        sm = boto3.client("sagemaker", region_name=region)
        params = {
            "TargetResources": ["training-job"],
            "InstanceType": f"ml.{itype}",
            "InstanceCount": int(count),
            "StartTimeAfter": datetime.combine(start_date, datetime.min.time()),
            "DurationHours": int(duration * 24)
        }
        if use_end_date and end_date:
            params["EndTimeBefore"] = datetime.combine(end_date, datetime.min.time())
        log_msg(f"SageMaker params: {params}", region, itype)

        resp = sm.search_training_plan_offerings(**params)
        log_msg(f"SageMaker API Response: {resp}", region, itype)
        offerings = resp.get("TrainingPlanOfferings", [])
        results = []
        for o in offerings:
            upfront_fee = f"${o.get('UpfrontFee','0')}"
            reserved_offerings = o.get("ReservedCapacityOfferings", [])
            parts_count = len(reserved_offerings)
            duration_hours = o.get("DurationHours", 0)
            
            if reserved_offerings:
                r = reserved_offerings[0]  # Use first offering for details
                start_dt, end_dt = parse_iso_date(r.get("StartTime")), parse_iso_date(r.get("EndTime"))
                instance_type_clean = r.get("InstanceType", itype).replace('ml.', '')
                results.append({
                    "Region": region, "Instance Type": r.get("InstanceType", itype),
                    "Instance Count": str(r.get("InstanceCount", 0)),
                    "Duration (days)": f"{duration_hours / 24:.2f}",
                    "Start Date": start_dt.strftime("%d/%m/%Y %H:%M") if start_dt else "N/A",
                    "End Date": end_dt.strftime("%d/%m/%Y %H:%M") if end_dt else "N/A",
                    "Upfront Fee": upfront_fee,
                    "Number of Parts": str(parts_count),
                    "Availability Zone": r.get("AvailabilityZone","N/A")
                })
        return results
    except Exception as e:
        if "InvalidAction" in str(e) or "AuthFailure" in str(e):
            return []
        return [{"Region": region, "Error": str(e)}]

# ----------------- Run Scans -----------------
col1, col2, col3 = st.columns([1, 1, 1])
with col1: do_capacity = st.button("Find EC2 Capacity Block")
with col2: do_sagemaker = st.button("Find SageMaker Training Plan")
with col3:
    do_timeline = st.button("📈 EC2 Capacity Block Timeline")
    with st.popover("ℹ️ About this feature"):
        st.markdown("""
**EC2 Capacity Block Timeline** shows, for each day over the next 8 weeks, the maximum number of instances you could reserve starting that day. It covers EC2 Capacity Blocks only — not SageMaker Training Plans.

**How it works:**
The chart is built by asking the AWS API a series of questions. If AWS offers a capacity block that runs for, say, 4 weeks, that one answer proves capacity is available on every day of those 4 weeks — so the tool asks about long reservations first to cover many days cheaply, then narrows down exactly which days the instance count goes up or down.

**Filters that apply:**
- ✅ Instance Type
- ✅ Region

**Filters that are ignored:**
- ❌ Duration — the tool chooses what to ask the API automatically
- ❌ Instance Count — the chart discovers the maximum available
- ❌ Start Date / End Date — always scans the full 8-week window

**Note:** AWS limits how often this API can be called (roughly 9 calls per minute), so a scan takes **about 1-2 minutes** per region/instance-type combination. Results are remembered for the session — running the same combination again is instant. Max 6 combinations at a time.
""")

def run_parallel(scan_fn, regions, instance_types, *args):
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(scan_fn, r, it, *args) for r in regions for it in instance_types]
        for f in concurrent.futures.as_completed(futures):
            results.extend(f.result())
    return results

# EC2 capacity search
if do_capacity:
    scan_regions = AWS_REGIONS if "All Regions" in selected_regions else selected_regions
    with st.spinner(f"Scanning {len(scan_regions)} region(s)..."):
        results = run_parallel(scan_region, scan_regions, selected_instance_types, instance_count, duration_days)
        success, errors = process_results(results, ["Region","Instance Type","Instance Count","Duration (days)","Start Date","End Date","Upfront Fee","Number of Parts","Availability Zone"])
        if success.empty:
            st.info("ℹ️ No capacity found. Retrying with reduced params...")
            reduced = run_parallel(scan_region, AWS_REGIONS, selected_instance_types, max(1,instance_count//2), max(1,duration_days//2))
            fallback, _ = process_results(reduced, ["Region","Instance Type","Instance Count","Duration (days)","Start Date","End Date","Part"])
            if not fallback.empty:
                st.success("✅ Found alternatives with reduced parameters!")
                st.dataframe(fallback, use_container_width=True)
            else:
                st.warning("⚠️ No offerings found even with reduced parameters.")
        else:
            st.success("✅ Capacity blocks found!")
            st.dataframe(success, width='stretch')
        if not errors.empty:
            st.warning("⚠️ Some regions returned errors:")
            st.dataframe(errors, width='stretch')

# SageMaker training plan
if do_sagemaker:
    scan_regions = AWS_REGIONS if "All Regions" in selected_regions else selected_regions
    with st.spinner(f"Scanning SageMaker in {len(scan_regions)} region(s)..."):
        results = run_parallel(scan_sagemaker_region, scan_regions, selected_instance_types, instance_count, duration_days)
        success, errors = process_results(results, ["Region","Instance Type","Instance Count","Duration (days)","Start Date","End Date","Upfront Fee","Number of Parts","Availability Zone"])
        if success.empty:
            st.info("ℹ️ No SageMaker offerings found.")
        else:
            st.success("✅ SageMaker offerings found!")
            st.dataframe(success, width='stretch')
        if not errors.empty:
            st.warning("⚠️ Some regions returned errors:")
            st.dataframe(errors, width='stretch')

# Capacity Timeline
if do_timeline:
    scan_regions = AWS_REGIONS if "All Regions" in selected_regions else selected_regions
    combos = [(r, it) for r in scan_regions for it in selected_instance_types]

    if len(combos) > MAX_TIMELINE_COMBOS:
        st.warning(f"⚠️ Too many combinations ({len(combos)}). Showing first {MAX_TIMELINE_COMBOS} only.")
        combos = combos[:MAX_TIMELINE_COMBOS]

    for region, itype in combos:
        st.subheader(f"{itype} — {region}")
        progress = st.progress(0.0, text=f"Scanning {region} / {itype}...")
        df, msg = scan_timeline(region, itype, progress_cb=lambda v: progress.progress(v))
        progress.empty()

        if df is None:
            st.info(f"ℹ️ {msg}")
            continue

        if msg:
            st.warning(msg)

        tab_chart, tab_details = st.tabs(["📈 Chart", "📋 Details"])

        with tab_chart:
            max_val = int(df["Max Instances"].max())
            area = alt.Chart(df).mark_area(
                interpolate="step-after",
                line={"color": "#2a78d6", "strokeWidth": 2},
                color=alt.Gradient(
                    gradient="linear",
                    stops=[
                        alt.GradientStop(color="rgba(42, 120, 214, 0.35)", offset=0),
                        alt.GradientStop(color="rgba(42, 120, 214, 0.08)", offset=1),
                    ],
                    x1=0, x2=0, y1=1, y2=0,
                ),
            ).encode(
                x=alt.X("Date:T", title="Date", axis=alt.Axis(format="%b %d", labelAngle=-45)),
                y=alt.Y("Max Instances:Q", title="Max Instances Available",
                         scale=alt.Scale(domain=[0, max(max_val + 4, 10)])),
                tooltip=[
                    alt.Tooltip("Date:T", format="%Y-%m-%d"),
                    alt.Tooltip("Max Instances:Q"),
                ],
            ).properties(height=320, width="container")

            zero_line = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
                color="#c3c2b7", strokeWidth=1
            ).encode(y="y:Q")

            st.altair_chart(area + zero_line, use_container_width=True)

            st.caption(
                "⚠️ **Note:** This data is a point-in-time snapshot. Capacity availability "
                "changes continuously. Instance counts are capped at 64 (the API maximum per "
                "block) — actual available capacity may exceed what is shown."
            )

        with tab_details:
            detail_df = df[df["Available"]].drop(columns=["Available"])
            if not detail_df.empty:
                detail_df["Date"] = detail_df["Date"].dt.strftime("%Y-%m-%d")
                st.dataframe(detail_df, use_container_width=True)
            else:
                st.info("No available dates found.")

