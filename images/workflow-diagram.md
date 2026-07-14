# PII Anonymization System — Workflow Diagrams

## Processing Pipeline

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#FFFFFF', 'primaryTextColor': '#232F3E', 'lineColor': '#232F3E', 'fontFamily': 'Amazon Ember, Helvetica, Arial, sans-serif'}}}%%
flowchart TD
    A["📄 Input<br/>PDF / Word / Excel / TXT / Image / Audio"] --> B{"File Type"}

    B -->|"Text-based<br/>PDF text, Word, Excel, TXT"| T1["Extract text<br/>chunk by lines"]
    B -->|"Vision-based<br/>PDF image, Image files"| V1["Convert to page images"]
    B -->|"Audio<br/>.mp3, .wav"| AU1["Amazon Transcribe<br/>speech → timestamped text<br/>+ speaker diarization"]

    T1 --> T2["Threaded Bedrock LLM<br/>PII detection"]
    V1 --> V2["Threaded Vision LLM<br/>+ Textract OCR per page"]
    V2 --> V3["Bounding box refinement<br/>Exact → Spatial → Fuzzy"]
    AU1 --> AU2["Amazon Bedrock LLM<br/>PII detection in transcript"]

    T2 --> C1["Cluster PII by entity<br/>value patterns + type hints"]
    V3 --> C1
    AU2 --> C1

    C1 --> C2{"Redaction mode"}
    C2 -->|"synthetic"| C3["Batch LLM replacements<br/>Faker fallback"]
    C2 -->|"blackout"| C4["Black rectangles<br/>skip LLM generation"]

    C3 --> D1{"Which pipeline?"}
    C4 --> D1

    D1 -->|"Text"| D2["Replace PII in text<br/>longest-first matching"]
    D1 -->|"Vision"| D3["Flatten-to-image PDF redaction"]
    D3 --> D4["Render page (pypdfium2)<br/>→ pixel redaction at Textract boxes<br/>→ assemble image-based PDF<br/>Images → pixel redact"]
    D1 -->|"Audio"| DA1["Amazon Polly synthetic speech<br/>or silence"]
    DA1 --> DA2["ffmpeg splice at<br/>PII timestamps"]

    D2 --> E1["Upload to S3<br/>redacted/text/ or .docx/.xlsx"]
    D4 --> E2["Upload to S3<br/>redacted/image/ or original format"]
    DA2 --> E3["Upload to S3<br/>redacted/audio/<br/>WAV + transcript"]

    E1 --> F["Store PII mappings<br/>in DynamoDB for audit trail"]
    E2 --> F
    E3 --> F

    F --> G(["✅ Process Complete"])

    style A fill:#E1D5E7,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style B fill:#FFF2CC,color:#232F3E,stroke:#232F3E,stroke-width:2px

    style T1 fill:#DAE8FC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style T2 fill:#DAE8FC,color:#232F3E,stroke:#232F3E,stroke-width:2px

    style V1 fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style V2 fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style V3 fill:#FFF2CC,color:#232F3E,stroke:#232F3E,stroke-width:2px

    style C1 fill:#F8CECC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style C2 fill:#FFF2CC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style C3 fill:#F8CECC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style C4 fill:#F8CECC,color:#232F3E,stroke:#232F3E,stroke-width:2px

    style D1 fill:#FFF2CC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style D2 fill:#DAE8FC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style D3 fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style D4 fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px

    style E1 fill:#E1D5E7,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style E2 fill:#E1D5E7,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style F fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style G fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px

    style AU1 fill:#FFE6CC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style AU2 fill:#FFE6CC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style DA1 fill:#FFE6CC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style DA2 fill:#FFE6CC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style E3 fill:#E1D5E7,color:#232F3E,stroke:#232F3E,stroke-width:2px
```

## Text-Based Approach (PDF text / Word / Excel / TXT)

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#FFFFFF', 'primaryTextColor': '#232F3E', 'lineColor': '#232F3E', 'fontFamily': 'Amazon Ember, Helvetica, Arial, sans-serif'}}}%%
flowchart TD
    A["📄 Document Input<br/>PDF / Word / Excel / TXT"] --> B["Extract text from document"]
    B --> C["Chunk text by lines<br/>token-aware splitting"]
    C --> D["Amazon Bedrock LLM<br/>Threaded PII detection<br/>N workers in parallel"]
    D --> E["PII detections list<br/>{content, type, confidence}"]

    E --> F["Cluster PII by entity<br/>value patterns + type hints"]
    F --> G["Amazon Bedrock LLM<br/>Batch synthetic generation<br/>one identity per cluster"]
    G --> H{"LLM success?"}
    H -->|"Yes"| I["PII mapping<br/>original → synthetic"]
    H -->|"No"| J["Faker fallback<br/>per item"]
    J --> I

    I --> K["Replace PII in text<br/>longest-first matching"]
    K --> L["Upload to S3<br/>redacted/text/"]
    L --> M["Store PII mappings<br/>DynamoDB audit trail"]
    M --> N(["✅ Complete"])

    style A fill:#E1D5E7,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style B fill:#DAE8FC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style C fill:#DAE8FC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style D fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style E fill:#FFF2CC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style F fill:#F8CECC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style G fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style H fill:#FFF2CC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style I fill:#F8CECC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style J fill:#F8CECC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style K fill:#DAE8FC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style L fill:#E1D5E7,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style M fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style N fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px
```

## Image-Based Approach (PDF image / standalone images)

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#FFFFFF', 'primaryTextColor': '#232F3E', 'lineColor': '#232F3E', 'fontFamily': 'Amazon Ember, Helvetica, Arial, sans-serif'}}}%%
flowchart TD
    A["📄 Document Input<br/>PDF / Image files"] --> B["Convert PDF to page images<br/>or load standalone image"]
    B --> C["Per page — threaded:"]

    C --> C1["① Amazon Textract<br/>OCR text + word coordinates"]
    C1 --> C2["② Amazon Bedrock Vision LLM<br/>Detect PII using image + OCR context"]
    C2 --> C3["③ Textract bounding box refinement<br/>Exact → Spatial → Fuzzy matching"]
    C3 --> D["PII detections list<br/>{content, type, bbox, confidence}"]

    D --> E["Cluster PII by entity<br/>value patterns + type hints"]
    E --> F{"Redaction mode"}
    F -->|"synthetic"| G["Amazon Bedrock LLM<br/>Batch synthetic generation<br/>Faker fallback"]
    F -->|"blackout"| H["Map all PII → black rectangles<br/>skip LLM generation"]

    G --> I["Render each page (pypdfium2)<br/>pixel redaction at Textract boxes<br/>flatten to image-based PDF"]
    H --> I

    I --> M["Upload to S3<br/>redacted/image/"]
    M --> N["Store PII mappings<br/>DynamoDB audit trail"]
    N --> O(["✅ Complete"])

    style A fill:#E1D5E7,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style B fill:#DAE8FC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style C fill:#DAE8FC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style C1 fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style C2 fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style C3 fill:#FFF2CC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style D fill:#FFF2CC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style E fill:#F8CECC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style F fill:#FFF2CC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style G fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style H fill:#F8CECC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style I fill:#FFF2CC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style J fill:#DAE8FC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style K fill:#DAE8FC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style L fill:#DAE8FC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style M fill:#E1D5E7,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style N fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style O fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px
```

## Audio-Based Approach (.mp3 / .wav)

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#FFFFFF', 'primaryTextColor': '#232F3E', 'lineColor': '#232F3E', 'fontFamily': 'Amazon Ember, Helvetica, Arial, sans-serif'}}}%%
flowchart TD
    A["🎙️ Audio Input<br/>.mp3 / .wav"] --> B["Amazon Transcribe<br/>transcription job from S3"]
    B --> C["Word-level timestamps<br/>+ speaker diarization"]
    C --> D["Amazon Bedrock LLM<br/>PII detection in transcript"]
    D --> E["PII detections list<br/>{content, type, confidence}"]

    E --> F["Cluster PII + batch synthetic<br/>generation (Faker fallback)"]
    F --> G{"Redaction mode"}
    G -->|"synthetic"| H["Amazon Polly<br/>synthesize replacement speech"]
    G -->|"silence"| I["Generate silence<br/>for the PII span"]

    H --> J["Map PII text → timed word spans"]
    I --> J
    J --> K["ffmpeg splice replacements<br/>at exact PII timestamps"]
    K --> L["Upload to S3<br/>redacted/audio/ — WAV + speaker transcript"]
    L --> M["Store PII mappings<br/>DynamoDB audit trail"]
    M --> N(["✅ Complete"])

    style A fill:#FFE6CC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style B fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style C fill:#FFF2CC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style D fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style E fill:#FFF2CC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style F fill:#F8CECC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style G fill:#FFF2CC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style H fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style I fill:#FFE6CC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style J fill:#DAE8FC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style K fill:#DAE8FC,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style L fill:#E1D5E7,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style M fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px
    style N fill:#D5E8D4,color:#232F3E,stroke:#232F3E,stroke-width:2px
```

## Trigger Flows

### Direct S3 → Lambda (no SQS)

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#232F3E', 'primaryTextColor': '#FFFFFF', 'primaryBorderColor': '#FF9900', 'lineColor': '#545B64', 'secondaryColor': '#37475A', 'tertiaryColor': '#F2F3F3', 'fontFamily': 'Amazon Ember, Helvetica, Arial, sans-serif'}}}%%
sequenceDiagram
    participant U as User
    participant S3 as S3 Bucket
    participant L as Lambda
    participant B as Bedrock
    participant T as Textract
    participant DB as DynamoDB

    U->>S3: Upload document
    S3->>L: S3 event notification
    L->>B: Step 1 — PII detection
    L->>T: Textract OCR (image-based only)
    L->>B: Step 2 — Synthetic generation
    L->>L: Step 3 — Redaction
    L->>S3: Upload redacted output
    L->>DB: Store PII mappings
```

### S3 → SQS → Lambda (with SQS)

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#232F3E', 'primaryTextColor': '#FFFFFF', 'primaryBorderColor': '#FF9900', 'lineColor': '#545B64', 'secondaryColor': '#37475A', 'tertiaryColor': '#F2F3F3', 'fontFamily': 'Amazon Ember, Helvetica, Arial, sans-serif'}}}%%
sequenceDiagram
    participant U as User
    participant S3 as S3 Bucket
    participant SQS as SQS Queue
    participant L as Lambda
    participant DDB as DynamoDB
    participant B as Bedrock
    participant T as Textract

    U->>S3: Upload document
    S3->>SQS: S3 event notification
    SQS->>L: Trigger Lambda (batch size 1)
    L->>DDB: Idempotency check (acquire lock)
    alt Already processed
        L-->>L: Skip (idempotent)
    else New document
        L->>B: Step 1 — PII detection
        L->>T: Textract OCR (image-based only)
        L->>B: Step 2 — Synthetic generation
        L->>L: Step 3 — Redaction
        L->>S3: Upload redacted output
        L->>DDB: Store PII mappings + release lock
    end
    Note over SQS,L: Failed messages → DLQ after 2 retries
```

## Clustering & Synthetic Generation Detail

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#232F3E', 'primaryTextColor': '#FFFFFF', 'primaryBorderColor': '#FF9900', 'lineColor': '#545B64', 'secondaryColor': '#37475A', 'tertiaryColor': '#F2F3F3', 'fontFamily': 'Amazon Ember, Helvetica, Arial, sans-serif'}}}%%
flowchart TD
    A["Raw PII detections<br/>[{content, type, confidence}, ...]"] --> B[Infer category per item]

    subgraph categorize["Category Inference (value-first)"]
        B --> B1{Value pattern?}
        B1 -->|"regex match"| B2["Phone / SSN / Date / Email<br/>Financial / Address"]
        B1 -->|"no match"| B3{Type hint map?}
        B3 -->|"config.yaml match"| B4[Mapped category]
        B3 -->|"no match"| B5[UNKNOWN — processed individually]
    end

    subgraph cluster["Cluster by Entity"]
        B2 --> C1[Normalize values<br/>strip punctuation, lowercase]
        B4 --> C1
        C1 --> C2[Group variants of same entity<br/>fuzzy match within category]
        C2 --> C3["Clusters: {entity → [variant1, variant2, ...]}"]
    end

    subgraph generate["Generate Synthetic"]
        C3 --> D1[Batch prompt: one identity per cluster]
        D1 --> D2{LLM response valid?}
        D2 -->|Yes| D3[Derive all variants from base synthetic]
        D2 -->|No| D4[Faker fallback per item]
        B5 --> D4
        D3 --> E["Final mapping: {original → synthetic}"]
        D4 --> E
    end

    style categorize fill:#e8f5e9
    style cluster fill:#fff9c4
    style generate fill:#fce4ec
```
