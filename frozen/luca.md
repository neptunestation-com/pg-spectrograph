# Lauca: Generating Application-Oriented Synthetic Workloads

*arxiv · 2019*

**Authors:** Yuming Li, Rong Zhang 0002, Yuchen Li, Shu Ke, Shuyan Zhang, [Aoying Zhou](/author/aoying-zhou/aut_3fad0df8f3fd44b3a90d3ef6dbe2e421)

## Summary

# Lauca: Generating Application-Oriented Synthetic Workloads

## Introduction
Testing database management systems (DBMS) often presents a conflict between realism and privacy. Real-world workloads provide the most accurate performance signals but are frequently inaccessible due to strict data privacy regulations like GDPR. Conversely, standard benchmarks like TPC-C are often "application-oblivious," failing to capture the unique nuances of specific production environments.

Lauca addresses this gap as a novel, application-oriented transactional workload generator. It creates "stunt double" workloads that mimic real-world application behavior with high fidelity—capturing transaction logic and data access patterns—without exposing sensitive underlying data.

## The Challenge of Realistic Benchmarking
When organizations evaluate new database technologies, they typically rely on three types of testing, each with significant drawbacks:
1. **Real Workloads:** High accuracy but restricted by privacy and security concerns.
2. **Standard Benchmarks:** Too generic to predict performance for unique, proprietary applications.
3. **Existing Synthetic Generators:** Often fail to model the causal relationships between transactions, leading to inaccurate predictions for critical metrics like deadlock frequency and cache hit ratios.

## System Architecture
Lauca operates by decoupling the **Production Environment**, where sensitive data resides, from the **Evaluation Environment**, where testing occurs. 

![The Lauca architecture separates production analysis from evaluation-time workload generation.](/figures/art_7df1b8e6de854f59bc67bae43c7d901d)
*[Lauca: Generating Application-Oriented Synthetic Workloads, page 2](/pdf/art_c3312549e5704f12a3d0569f1a5cc306#page=2)*

The system employs a dual-track approach:
*   **Workload Generation:** Analyzes transaction traces to extract logic and access distributions.
*   **Database Generation:** Extracts data characteristics from the real database to build a statistically similar test database.

## Methodology: Capturing the "Why" Behind Transactions
Lauca’s effectiveness stems from its ability to characterize workloads across four dimensions:

### 1. Transaction Templates and Logic
Instead of logging every SQL statement, Lauca generates "sketches" of transactions where parameters are symbolized. Its "secret sauce" is the analysis of **Transaction Logic**—identifying how parameters in one statement depend on the results of previous ones (e.g., an `UPDATE` using an ID returned by a `SELECT`).

![Lauca uses transaction templates (TX1-TX3) to symbolize SQL statements and capture inter-statement logic.](/figures/art_cb4d725846cb460391bc00aaded84ab0)
*[Lauca: Generating Application-Oriented Synthetic Workloads, page 3](/pdf/art_c3312549e5704f12a3d0569f1a5cc306#page=3)*

### 2. Data Access Distribution
To replicate realistic "hot" and "cold" data patterns, Lauca utilizes three distinct models:
*   **S-Dist (Skewness):** Models the frequency of access for specific data items.
*   **D-Dist (Dynamics):** Tracks how access patterns evolve over time.
*   **C-Dist (Continuity):** Measures the repetition rate of data access across time windows, which is critical for simulating realistic **cache hit ratios**.

## Key Insights
*   **Logic Drives Conflict:** Causal relationships between parameters are the primary drivers of transaction conflicts. Without understanding this logic, synthetic workloads cannot accurately reproduce real-world deadlocks or lock contention.
*   **The Continuity Factor:** Many generators treat time windows in isolation. By modeling continuity (C-Dist), Lauca ensures that the database cache behaves realistically, reflecting how applications often revisit the same data points over short intervals.
*   **Privacy-Preserving Abstraction:** By exporting only logic and statistics rather than raw data, Lauca allows vendors to troubleshoot performance issues without compromising client confidentiality.

## Performance and Results
Evaluated against benchmarks like TPC-C, SmallBank, and YCSB on MySQL and PostgreSQL, Lauca demonstrated exceptional fidelity:
*   **Accuracy:** Performance metrics (throughput, latency, CPU, and I/O) typically deviated by **less than 10%** from real workloads.
*   **Throughput Fidelity:** In PostgreSQL TPC-C tests, throughput deviation was as low as **2.5%**.
*   **Efficiency:** The logic extraction process is highly performant, processing $10^4$ instances in approximately **2.1 seconds**.

## Conclusion
Lauca represents a significant advancement in database benchmarking. By focusing on transaction logic and temporal data continuity, it provides a scalable, privacy-preserving method for generating high-fidelity synthetic workloads. Future work aims to address more complex mathematical dependencies between parameters and further automate configuration tuning.

## Related Directions

- [Privacy-Preserving Reasoning via Logic-Data Decoupling](/direction/privacy-preserving-reasoning-via-logic-data-decoupling-3991) - 2 concept elements
- [Multi-scale Temporal Partitioning for Sequential Representation](/direction/multi-scale-temporal-partitioning-for-sequential-representation-22012) - 1 concept elements

## Related Papers

- [LLM as an Algorithmist: Enhancing Anomaly Detectors via Programmatic Synthesis](/paper/llm-as-an-algorithmist-enhancing-anomaly-detectors-via-programmatic-synthesis/art_c77cdafc5254ca4fdef86fe577e88efc) (iclr · 2026)
- [Collaborative LLM Numerical Reasoning with Local Data Protection](/paper/collaborative-llm-numerical-reasoning-with-local-data-protection/art_7ae2bae5d54cb6bcd9597991ff5a09ed) (arxiv · 2025)
- [FACTS: Table Summarization via Offline Template Generation with Agentic Workflows](/paper/facts-table-summarization-via-offline-template-generation-with-agentic-workflows/art_82d041d2d019348c2ebb9ee4670dd6cd) (arxiv · 2025)
- [LLM as an Algorithmist: Enhancing Anomaly Detectors via Programmatic Synthesis](/paper/llm-as-an-algorithmist-enhancing-anomaly-detectors-via-programmatic-synthesis/art_162b6b7701a4f6de93e703e57743f152) (arxiv · 2025)
- [FlowMind: Automatic Workflow Generation with LLMs](/paper/flowmind-automatic-workflow-generation-with-llms/art_2311f2b5eea2fe4ae68ea96d20e394ed) (arxiv · 2024)
- [Multi-Scale Anomaly Detection for Time Series with Attention-based Recurrent Autoencoders](/paper/multi-scale-anomaly-detection-for-time-series-with-attention-based-recurrent/art_ba8e749825cc208f15ac6d8a2ba3fea4) (ACML · 2022)
- [E.M.Ground: A Temporal Grounding Vid-LLM with Holistic Event Perception and Matching](/paper/e-m-ground-a-temporal-grounding-vid-llm-with-holistic-event-perception-and/art_05633d7e310f32df88889f337059ebcf) (arXiv · 2026)
- [Edit-Based Flow Matching for Temporal Point Processes](/paper/edit-based-flow-matching-for-temporal-point-processes/art_963f20999c46f072a5bdd4760f2fd115) (iclr · 2026)
- [Enhancing guidance for missing data in diffusion-based sequential recommendation](/paper/enhancing-guidance-for-missing-data-in-diffusion-based-sequential-recommendation/art_fc58f88e6d0760c522f3fab46ba95b64) (arXiv · 2026)
- [PaAno: Patch-Based Representation Learning for Time-Series Anomaly Detection](/paper/paano-patch-based-representation-learning-for-time-series-anomaly-detection/art_7a6f78a24df399a3aa7b55377ffd8403) (iclr · 2026)
- [Scaling Goal-conditioned Reinforcement Learning with Multistep Quasimetric Distances](/paper/scaling-goal-conditioned-reinforcement-learning-with-multistep-quasimetric/art_78b70df5742b9370ba0a882cfa6611de) (iclr · 2026)
- [TINNs: Time-Induced Neural Networks for Solving Time-Dependent PDEs](/paper/tinns-time-induced-neural-networks-for-solving-time-dependent-pdes/art_6977abc6530ff5cfd9a63dd286a643b2) (icml · 2026)

## Concept Elements

- **mechanism:** Privacy-preserving workload abstraction separates the extraction of transaction logic and access statistics from the underlying sensitive data. This allows for the creation of high-fidelity synthetic workloads that can be safely shared and executed in isolated evaluation environments for performance tuning. _(Direction: Privacy-Preserving Reasoning via Logic-Data Decoupling)_
- **finding:** Abstracting workload characteristics into transaction templates and statistical distributions enables high-fidelity performance evaluation while preserving data privacy. This separation of logic from actual data values allows organizations to share workload profiles with external vendors for troubleshooting and optimization without violating security regulations. _(Direction: Privacy-Preserving Reasoning via Logic-Data Decoupling)_
- **limitation:** The effectiveness of workload characterization often depends on the manual selection of analysis parameters, such as the temporal window size for data access patterns. Automating the discovery of these optimal configurations remains an open challenge to make synthetic generation accessible to non-experts. _(Direction: Multi-scale Temporal Partitioning for Sequential Representation)_
- **finding:** Synthetic workloads that model transaction logic and data access distributions can achieve performance metrics within 10% of real-world applications across throughput, latency, and resource utilization. This high level of accuracy allows for reliable database behavior prediction under production-like conditions without using actual production data.
- **problem:** Evaluating database performance using real-world production data is often prohibited by strict privacy regulations and security concerns. This creates a barrier for developers who need to test system behavior under realistic conditions without exposing sensitive information.
- **problem:** Synthetic workload generators frequently overlook the logical dependencies between transaction parameters, such as when one operation's input depends on a previous operation's output. Without capturing this logic, synthetic tests fail to replicate real-world performance bottlenecks like lock contention and deadlocks.
- **mechanism:** Inter-statement dependency modeling identifies logical relationships, such as equality or linear correlations, between parameters in a sequence of database operations. Mapping these dependencies ensures that synthetic transactions maintain the causal links necessary to trigger realistic lock contentions and deadlocks.
- **limitation:** Transaction logic analysis frequently focuses on pairwise relationships between data items, failing to capture dependencies involving three or more variables. This gap can lead to inaccurate synthetic workloads for systems where transaction outcomes depend on the intersection of multiple distinct data points.
- **mechanism:** Symbolic transaction templating replaces specific data values in database queries with placeholders to create generalized sketches of transaction logic. This abstraction allows a generator to produce synthetic traffic that mirrors the structural variety of a production workload without relying on the original data.
- **limitation:** Current workload generation techniques often struggle to model non-linear mathematical dependencies between transaction parameters, such as quadratic or exponential relationships. This limitation prevents the accurate simulation of applications where data values are derived through complex arithmetic logic.
- **limitation:** Many workload simulation tools treat distinct time intervals as independent, failing to account for the repetition of data access over time. This lack of temporal continuity results in unrealistic cache hit ratios, as the simulation does not accurately mimic how data remains hot or cold across windows.
- **limitation:** Generic industry benchmarks often fail to reflect the unique data access patterns and transaction logic of specific applications. Consequently, these standard tests cannot accurately predict how a database will perform under a specialized or proprietary workload.
- **mechanism:** Temporal access continuity modeling measures the rate at which specific data points are repeatedly accessed across different time intervals. By incorporating this metric, synthetic workload generators can accurately replicate the cache hit ratios and memory pressure observed in real-world applications.
- **finding:** Incorporating temporal continuity into data access models ensures that synthetic workloads maintain realistic cache hit ratios by mimicking how frequently data is repeated across time windows. This approach prevents the inaccuracy common in generators that treat distinct time intervals as independent, which often leads to underestimating database performance.
- **finding:** Modeling the logical dependencies between transaction parameters is necessary to accurately simulate concurrency issues like lock contention and deadlocks. Without capturing how subsequent operations depend on the results of previous ones, synthetic generators fail to replicate the resource conflicts observed in real-world database environments.

## Versions

- [Lauca: Generating Application-Oriented Synthetic Workloads](https://lacuna.tiptreesystems.com/work/lauca-generating-application-oriented-synthetic-workloads/wrk_11bb12c49a037ea0107ab1c0ac4eb7f9) (arxiv · 2019 · presentation)
- [Lauca: Generating Application-Oriented Synthetic Workloads](https://lacuna.tiptreesystems.com/work/lauca-generating-application-oriented-synthetic-workloads/wrk_11bb12c49a037ea0107ab1c0ac4eb7f9/version/art_f2555c77a78916bd86252857df38a056) (arXiv · 2019)
