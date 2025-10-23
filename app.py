import streamlit as st
import boto3
import pandas as pd
from datetime import datetime, timedelta
import concurrent.futures
import inspect

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
    "p6-b200.48xlarge",
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
col1, col2, col3 = st.columns([1, 1, 4])
with col1: do_capacity = st.button("Find EC2 Capacity Block")
with col2: do_sagemaker = st.button("Find SageMaker Training Plan")

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

