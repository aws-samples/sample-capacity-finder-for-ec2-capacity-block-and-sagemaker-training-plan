---
name: capacity-timeline-demo
description: Set up and demo the EC2 Capacity Block Timeline feature from PR #5 of the aws-samples capacity finder. Use when someone wants to replicate the capacity-timeline branch locally, review the PR, or see a walkthrough of what the timeline chart adds over the existing search buttons.
---

# Demo: EC2 Capacity Block Timeline (PR #5)

This skill sets up and demonstrates the **EC2 Capacity Block Timeline** feature
proposed in [PR #5](https://github.com/aws-samples/sample-capacity-finder-for-ec2-capacity-block-and-sagemaker-training-plan/pull/5).
It adds a third button to the capacity finder that charts, for every day over
the next 8 weeks, the maximum number of instances that could be reserved as an
EC2 Capacity Block — an at-a-glance planning view, instead of querying one
instance-count/duration combination at a time.

## Important: why you may not see the feature

The README's recommended `uvx` one-liner streams `app.py` from the **main
branch** raw GitHub URL. PR branches never show up that way. To see this
feature you must run a **local checkout of the PR branch** — that is what the
setup below does.

## Step 1 — Get the PR branch

In an existing clone of the upstream repo (easiest):

```bash
gh pr checkout 5
```

Or without the GitHub CLI:

```bash
git fetch origin pull/5/head:capacity-timeline
git checkout capacity-timeline
```

Or as a fresh clone from the contributor's fork:

```bash
git clone -b capacity-timeline https://github.com/lazydragon/sample-capacity-finder-for-ec2-capacity-block-and-sagemaker-training-plan.git
cd sample-capacity-finder-for-ec2-capacity-block-and-sagemaker-training-plan
```

Verify you have the feature: `grep -c "Capacity Block Timeline" app.py` should
print a non-zero number.

## Step 2 — Credentials

Any AWS credentials with `ec2:DescribeCapacityBlockOfferings` permission
(read-only is enough for the timeline; the existing SageMaker button
additionally uses `sagemaker:SearchTrainingPlanOfferings`).

```bash
aws sts get-caller-identity   # confirm credentials resolve
```

## Step 3 — Run the local checkout

```bash
uvx --with boto3==1.40.18 --with pandas==2.3.2 --from streamlit==1.49.0 streamlit run app.py
```

(Same dependencies as the README one-liner, but running the local file.)
Or, with pip: `pip install -r requirements.txt && streamlit run app.py`.

The app opens at http://localhost:8501 with **three** buttons; the new one is
"📈 EC2 Capacity Block Timeline".

## Step 4 — Demo walkthrough

1. In the sidebar select **one** instance type and **one** region. A
   known-good example at the time of writing: `p5.4xlarge` + `ap-south-1`
   (use any pool your account has visibility into — supported combinations
   are listed in the EC2 Capacity Blocks docs).
2. Click **📈 EC2 Capacity Block Timeline**.
3. Expect a progress bar for **about 1–2 minutes**. This is deliberate:
   `DescribeCapacityBlockOfferings` has a very small API quota (10-call
   burst, then ~1 call per 7 seconds, per account per region), and the scan
   paces itself to stay under it.
4. Read the chart:
   - X-axis: every day for the next 8 weeks. Y-axis: the **maximum instance
     count purchasable** as a Capacity Block starting that day.
   - The staircase shape is real data — capacity typically ramps up as the
     start date moves further out.
   - The **Details** tab lists the same data as a table.
5. Click the button again with the same selection: the result is **instant**
   (responses are cached for the app session).

## What to compare it against (the benefit)

With the existing "Find EC2 Capacity Block" button you must guess an instance
count and duration, and you get back only the earliest matching offering — one
data point per query. To answer "when can I get capacity and how much?" a user
has to run many searches by hand.

The timeline answers that in one click. Under the hood it exploits a property
of the API: an offering of duration D at count N starting day S proves count
≥ N on every one of those D days — so one long-duration probe can confirm
weeks of availability, and the exact per-day staircase is recovered in
~15–25 API calls instead of ~110.

## Troubleshooting

- **Third button missing** → you are running main (e.g. via the README
  one-liner or a stale clone). Re-do Step 1 and run the local file.
- **"No capacity found in the searchable window"** → that pool genuinely has
  no offerings, or your account can only search a near-term window in that
  region (some accounts are restricted; try another region/instance type).
- **"API describe limit reached" warning** → the account's quota was already
  drained by other callers; the chart shows what was proven so far. Wait a
  minute or two and rerun.
- **Scan feels slow** → expected; see Step 4.3. The per-region quota also
  means scans of the *same region* for different instance types share the
  budget and queue behind each other.
