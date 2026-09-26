# Pricing Reference

Static rate table given to the agent as **context, not a tool**. The agent writes
its own cost-calculation script against these rates and runs it in the sandbox;
this file is never called directly by the harness.

| Resource | Rate |
| --- | --- |
| EBS gp3 | $0.08 / GB-month |
| EBS gp2 | $0.10 / GB-month |
| EBS io1 | $0.125 / GB-month |
| Application Load Balancer (idle, base) | ~$16.20 / month |
| Stopped EC2 instance | $0 compute, but attached EBS volumes keep billing at the rates above |

Notes:

- These are `us-east-1` on-demand list-price approximations for the demo account's region; not live pricing API data (Cost Explorer is out of scope, PRD §2).
- Attached-volume cost for a stopped instance = volume size (GB) × the volume's rate above, prorated monthly.
- ALB idle base rate assumes no LCU usage (no live traffic on an idle load balancer).
