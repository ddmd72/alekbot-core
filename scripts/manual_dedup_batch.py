#!/usr/bin/env python3
"""
Manual deduplication script - processes suspect groups and removes duplicates
Based on semantic analysis of fact completeness and specificity
"""

# Batch 3 decisions (Groups 31-50)
DECISIONS_BATCH_3 = {
    # Group 31-32: Activity level variants - Keep B (cleaner)
    ("As of the December 2024 report, the patient's activity level was low.",
     "The patient's activity level was reported as low as of December 2024."): "B",
    ("As of the December 2024 report, the patient's activity level was low.",
     "The patient's reported activity level was low as of December 2024."): "B",
    
    # Group 33: Bilateral kidney stones date variants - Keep A (earlier valid_from)
    ("Bilateral kidney stones (approximately 0.5 cm) were diagnosed between 2020 and 2021.",
     "Bilateral kidney stones (approximately 0.5 cm) were diagnosed in 2020 or 2021."): "A",
    
    # Group 34: Bilateral kidney stones - Keep A (includes date range)
    ("Bilateral kidney stones (approximately 0.5 cm) were diagnosed between 2020 and 2021.",
     "Bilateral kidney stones (approximately 0.5cm) were diagnosed."): "A",
    
    # Group 35: Bilateral kidney stones notation - Keep A (first variant)
    ("Bilateral kidney stones (approximately 0.5 cm) were diagnosed between 2020 and 2021.",
     "Bilateral kidney stones (~0.5cm) were diagnosed between 2020 and 2021."): "A",
    
    # Group 36: Bilateral kidney stones - Keep A (includes date)
    ("Bilateral kidney stones (approximately 0.5 cm) were diagnosed in 2020 or 2021.",
     "Bilateral kidney stones (approximately 0.5cm) were diagnosed."): "A",
    
    # Group 37: Bilateral kidney stones - Keep A (has specific year)
    ("Bilateral kidney stones (approximately 0.5 cm) were diagnosed in 2020 or 2021.",
     "Bilateral kidney stones (~0.5cm) were diagnosed between 2020 and 2021."): "A",
    
    # Group 38: Bilateral kidney stones - Keep B (date range)
    ("Bilateral kidney stones (approximately 0.5cm) were diagnosed.",
     "Bilateral kidney stones (~0.5cm) were diagnosed between 2020 and 2021."): "B",
    
    # Group 39: Blood pressure - Keep B (more specific date format)
    ("Blood pressure was 122/85 mmHg during a pre-op assessment.",
     "On 2025-09-12 (Pre-op), the patient's blood pressure was 122/85 mmHg."): "B",
    
    # Group 40-41: Creatinine - Keep B (specific date)
    ("Creatinine was high at 111 µmol/L.",
     "On January 4, 2025, Creatinine was high at 111 µmol/L."): "B",
    ("Creatinine was high at 111 µmol/L.",
     "On January 4, 2025, the Creatinine level was high at 111 µmol/L."): "B",
    
    # Group 42: Crunches forbidden - Keep A (earlier date)
    ("Crunches (Скручування) are strictly forbidden due to the patient's back lever biomechanics.",
     "Crunches (Скручування) are strictly forbidden for the patient due to their back lever biomechanics."): "A",
    
    # Group 43: Crunches biomechanics - Keep A (simpler)
    ("Crunches (Скручування) are strictly forbidden due to the patient's back lever biomechanics.",
     "Sit-ups (crunches) are biomechanically disadvantaged for the patient and are forbidden due to their long back lever."): "A",
    
    # Group 44: Crunches forbidden contraindications - Keep A (simpler)
    ("Crunches (Скручування) are strictly forbidden due to the patient's back lever biomechanics.",
     "The patient is forbidden from performing crunches (Скручування) due to contraindications related to back lever biomechanics."): "A",
    
    # Group 45: Crunches for patient - Keep B (more detailed)
    ("Crunches (Скручування) are strictly forbidden for the patient due to their back lever biomechanics.",
     "Sit-ups (crunches) are biomechanically disadvantaged for the patient and are forbidden due to their long back lever."): "B",
    
    # Group 46: Crunches variants - Keep A (cleaner)
    ("Crunches (Скручування) are strictly forbidden for the patient due to their back lever biomechanics.",
     "The patient is forbidden from performing crunches (Скручування) due to contraindications related to back lever biomechanics."): "A",
    
    # Group 47: Birth date language - Keep A (English)
    ("Dmytro Deleur was born on June 13, 1972.",
     "Dmytro Deleur народився 13 червня 1972 року."): "A",
    
    # Group 48: Birth date variants - Keep A (natural phrasing)
    ("Dmytro Deleur was born on June 13, 1972.",
     "Patient Dmytro Deleur was born on 1972-06-13."): "A",
    
    # Group 49: Uric acid variants - Keep B (most specific)
    ("Dmytro had an alert for High Uric Acid, measured at 8.9 mg/dL in March 2025, posing a risk of gout and stones.",
     "The patient had a high Uric Acid level (8.9 mg/dL) in March 2025, indicating a risk of gout and stones."): "B",
    
    # Group 50: Reactive Pancreatitis - Keep B (history mentioned)
    ("Dmytro has a condition of Reactive Pancreatitis, which is currently dormant and sensitive to triggers.",
     "The patient has a history of Reactive Pancreatitis, which is currently dormant but sensitive to triggers."): "B",
}

# Batch 2 decisions (Groups 11-30) - PROCESSED
DECISIONS_BATCH_2_DONE = {
    # Group 11: Hematology - Keep B (more specific with date)
    ("All hematology parameters were within the normal range.",
     "As of 2025-03-28, all hematology parameters were within the normal range."): "B",
    
    # Group 12: Uric Acid alert - Keep A (includes risk context)
    ("An alert exists regarding high Uric Acid (8.9 mg/dL) measured in March 2025, posing a risk of gout or stones.",
     "Dmytro had an alert for High Uric Acid, measured at 8.9 mg/dL in March 2025, posing a risk of gout and stones."): "A",
    
    # Group 13: Uric Acid - Keep A (alert status mentioned)
    ("An alert exists regarding high Uric Acid (8.9 mg/dL) measured in March 2025, posing a risk of gout or stones.",
     "The patient had a high Uric Acid level (8.9 mg/dL) in March 2025, indicating a risk of gout and stones."): "A",
    
    # Group 14: ECG QTc - Keep A (pre-operative context)
    ("An ECG on September 12, 2025, showed a QTc of 402 ms, with no pre-operative contraindications noted.",
     "ECG showed a QTc of 402 ms, with no medical contraindications noted."): "A",
    
    # Group 15: ECG PR - Keep A (complete phrasing)
    ("An ECG on September 12, 2025, showed Sinus Rhythm, with a PR interval (~196 ms) near the upper limit of normal.",
     "ECG showed a PR interval of approximately 196 ms, which is at the upper limit of normal."): "A",
    
    # Group 16: ECG PR variations - Keep A (most complete)
    ("An ECG on September 12, 2025, showed Sinus Rhythm, with a PR interval (~196 ms) near the upper limit of normal.",
     "The ECG performed on 2025-09-12 showed a PR interval of approximately 196 ms, noted as the upper limit of normal."): "A",
    
    # Group 17: ECG PR variations - Keep A
    ("An ECG on September 12, 2025, showed Sinus Rhythm, with a PR interval (~196 ms) near the upper limit of normal.",
     "The ECG performed on September 12, 2025, showed a PR interval of approximately 196 ms (upper limit of normal)."): "A",
    
    # Group 18: Approved proteins - Keep B (more recent)
    ("Approved protein sources for the patient include Venison (Козуля), Mackerel, and Salmon.",
     "Approved protein sources include Venison (Козуля), Mackerel, and Salmon."): "B",
    
    # Group 19: Approved proteins variants - Keep A (for the patient)
    ("Approved protein sources for the patient include Venison (Козуля), Mackerel, and Salmon.",
     "Approved proteins for Dmytro's diet include Venison (Козуля), Mackerel, and Salmon."): "A",
    
    # Group 20: Approved proteins - Keep A (earlier date = original)
    ("Approved protein sources for the patient include Venison (Козуля), Mackerel, and Salmon.",
     "Approved proteins for the patient include Venison (Козуля), Mackerel, and Salmon."): "A",
    
    # Group 21: Approved proteins language variant - Keep B (more standard)
    ("Approved protein sources include Venison (Козуля), Mackerel, and Salmon.",
     "Approved proteins for Dmytro's diet include Venison (Козуля), Mackerel, and Salmon."): "B",
    
    # Group 22: Approved proteins - Keep B (cleaner wording)
    ("Approved protein sources include Venison (Козуля), Mackerel, and Salmon.",
     "Approved proteins for the patient include Venison (Козуля), Mackerel, and Salmon."): "B",
    
    # Group 23: Approved proteins variants - Keep B (for the patient)
    ("Approved proteins for Dmytro's diet include Venison (Козуля), Mackerel, and Salmon.",
     "Approved proteins for the patient include Venison (Козуля), Mackerel, and Salmon."): "B",
    
    # Group 24: Weight March 2025 - Keep A (includes location: Puzol, Spain)
    ("Around March 2025, the patient's weight was approximately 83 kg (recorded in Puzol, Spain), representing a 15 kg loss post-diet.",
     "Dmytro's weight was approximately 83 kg (Post-diet result, -15kg) in March 2025 in Puzol, Spain."): "A",
    
    # Group 25: Weight achievement - Keep A (includes location)
    ("Around March 2025, the patient's weight was approximately 83 kg (recorded in Puzol, Spain), representing a 15 kg loss post-diet.",
     "The patient achieved a weight of approximately 83 kg in March 2025, representing a 15 kg loss post-diet."): "A",
    
    # Group 26: Weight variants - Keep A (most complete)
    ("Around March 2025, the patient's weight was approximately 83 kg (recorded in Puzol, Spain), representing a 15 kg loss post-diet.",
     "The patient weighed approximately 83 kg in March 2025 (Puzol, Spain), following a 15 kg weight loss."): "A",
    
    # Group 27: Weight post-diet - Keep A (includes location)
    ("Around March 2025, the patient's weight was approximately 83 kg (recorded in Puzol, Spain), representing a 15 kg loss post-diet.",
     "The patient weighed approximately 83 kg in March 2025, representing a total weight loss of 15 kg post-diet."): "A",
    
    # Group 28: Activity level Dec 2024 - Keep A (more complete)
    ("As of December 2024, the patient's reported physical activity level was low.",
     "As of the December 2024 report, the patient's activity level was low."): "A",
    
    # Group 29: Activity level variants - Keep A
    ("As of December 2024, the patient's reported physical activity level was low.",
     "The patient's activity level was reported as low as of December 2024."): "A",
    
    # Group 30: Activity level - Keep A
    ("As of December 2024, the patient's reported physical activity level was low.",
     "The patient's reported activity level was low as of December 2024."): "A",
}

# Batch 1 decisions (Groups 1-10) - PROCESSED
DECISIONS_BATCH_1_DONE = {
    # Group 1: CT scan Angers - Keep B (more specific: "in January 2025")
    ("A CT Scan in Angers found no kidney stones", "A CT scan in Angers in January 2025 found no stones"): "B",
    
    # Group 2: CT scan 40mm cyst - Keep B (more details: "Bosniak I (Benign)")
    ("A CT scan on December 31, 2024, identified a 40 mm Simple Cyst on the Left Kidney.", 
     "A CT Scan on December 31, 2024, identified a Simple Cyst (40 mm) on the Left Kidney, classified as Bosniak I (Benign)."): "B",
    
    # Group 3: CT scan showing observation requirement - Keep B (state + complete)
    ("A CT scan on December 31, 2024, identified a 40 mm Simple Cyst on the Left Kidney.",
     "A CT scan on December 31, 2024, showed a 40 mm Simple Cyst on the Left Kidney, classified as Bosniak I (Benign), requiring observation only."): "B",
    
    # Group 4: CT scan classification - Keep B (state type + classified)
    ("A CT scan on December 31, 2024, identified a 40 mm Simple Cyst on the Left Kidney.",
     "A CT Scan performed on 2024-12-31 identified a 40 mm Simple Cyst on the Left Kidney, classified as Bosniak I (Benign)."): "B",
    
    # Group 5: CT scan observation - Keep B (requiring observation only)
    ("A CT Scan on December 31, 2024, identified a Simple Cyst (40 mm) on the Left Kidney, classified as Bosniak I (Benign).",
     "A CT scan on December 31, 2024, showed a 40 mm Simple Cyst on the Left Kidney, classified as Bosniak I (Benign), requiring observation only."): "B",
    
    # Group 6: CT scan state comparison - Keep B (state + most complete)
    ("A CT Scan on December 31, 2024, identified a Simple Cyst (40 mm) on the Left Kidney, classified as Bosniak I (Benign).",
     "A CT Scan performed on 2024-12-31 identified a 40 mm Simple Cyst on the Left Kidney, classified as Bosniak I (Benign)."): "B",
    
    # Group 7: State vs state - Keep A (requiring observation only)
    ("A CT scan on December 31, 2024, showed a 40 mm Simple Cyst on the Left Kidney, classified as Bosniak I (Benign), requiring observation only.",
     "A CT Scan performed on 2024-12-31 identified a 40 mm Simple Cyst on the Left Kidney, classified as Bosniak I (Benign)."): "A",
    
    # Group 8: Observation requirement - Keep A (more complete)
    ("A CT scan on December 31, 2024, showed a 40 mm Simple Cyst on the Left Kidney, classified as Bosniak I (Benign), requiring observation only.",
     "A CT scan revealed a simple, benign (Bosniak I) cyst measuring 40 mm on the left kidney, which requires observation only."): "A",
    
    # Group 9: Event vs simple cyst - Keep A (CT scan source mentioned)
    ("A CT scan revealed a simple, benign (Bosniak I) cyst measuring 40 mm on the left kidney, which requires observation only.",
     "I have a simple 40mm cyst on my left kidney, classified as Bosniak I (benign) and requiring only observation."): "A",
    
    # Group 10: Hematology parameters - Keep A (includes specific parameters list)
    ("All hematology parameters measured on March 28, 2025 (including Hemoglobin, Platelets, and Leucocytes), were within the normal range.",
     "As of 2025-03-28, all hematology parameters were within the normal range."): "A",
}

def find_fact_in_file(content: str, fact_snippet: str) -> str | None:
    """Find fact line in file content by matching beginning of text"""
    lines = content.split('\n')
    for line in lines:
        if line.strip().startswith('- **['):
            # Extract fact text after type marker
            if fact_snippet in line:
                return line
    return None

def remove_fact(content: str, fact_line: str) -> str:
    """Remove fact line from content"""
    lines = content.split('\n')
    new_lines = [line for line in lines if line.strip() != fact_line.strip()]
    return '\n'.join(new_lines)

def process_batch(decisions: dict, batch_name: str, input_file: str, output_file: str):
    """Process a batch of deduplication decisions"""
    
    # Read input file
    with open(input_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    facts_removed = 0
    
    for (fact_a_snippet, fact_b_snippet), keep in decisions.items():
        if keep == "B":
            # Remove Fact A
            fact_line = find_fact_in_file(content, fact_a_snippet[:50])
            if fact_line:
                content = remove_fact(content, fact_line)
                facts_removed += 1
                print(f"✅ Removed: {fact_a_snippet[:60]}...")
        else:
            # Remove Fact B
            fact_line = find_fact_in_file(content, fact_b_snippet[:50])
            if fact_line:
                content = remove_fact(content, fact_line)
                facts_removed += 1
                print(f"✅ Removed: {fact_b_snippet[:60]}...")
    
    # Write updated file
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(content)
    
    # Count remaining facts
    remaining_facts = len([line for line in content.split('\n') if line.strip().startswith('- **[')])
    
    print(f"\n🎉 {batch_name} complete:")
    print(f"   Facts removed: {facts_removed}")
    print(f"   Facts remaining: {remaining_facts}")
    print(f"   📄 Saved to: {output_file}")

if __name__ == '__main__':
    print("🔧 Processing Batch 3 (Groups 31-50)...")
    process_batch(
        decisions=DECISIONS_BATCH_3,
        batch_name="Batch 3",
        input_file='reports/account_facts_deduplicated.md',
        output_file='reports/account_facts_deduplicated.md'
    )
