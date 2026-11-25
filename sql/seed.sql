-- Cataloghi iniziali. Si possono ampliare liberamente (INSERT in cied.condition).
-- Codifica scelta: ICD-10-IM (modifica italiana dell'ICD-10, obbligatoria nelle SDO
-- per i dimessi dal 1/1/2027). I codici qui sotto sono a livello di categoria /
-- sottocategoria ICD-10 e vanno verificati sulla lista ministeriale ICD-10-IM, che
-- può richiedere un livello di dettaglio maggiore. Lo schema accetta anche
-- ICD-9-CM (coding_system = 'ICD-9-CM') per i dati storici.

-- Migrazione dei database creati con la versione precedente (coding_system 'ICD-10'):
UPDATE cied.condition c SET coding_system = 'ICD-10-IM'
 WHERE c.coding_system = 'ICD-10'
   AND NOT EXISTS (SELECT 1 FROM cied.condition x WHERE x.coding_system = 'ICD-10-IM' AND x.code = c.code);

INSERT INTO cied.condition (coding_system, code, label) VALUES
    ('LOCAL',  'NONE',  'Nessuna patologia pregressa'),
    -- aritmie e disturbi di conduzione
    ('ICD-10-IM', 'I48',   'Fibrillazione e flutter atriale'),
    ('ICD-10-IM', 'I49.5', 'Malattia del nodo del seno'),
    ('ICD-10-IM', 'I44.1', 'Blocco atrioventricolare di secondo grado'),
    ('ICD-10-IM', 'I44.2', 'Blocco atrioventricolare completo'),
    ('ICD-10-IM', 'I47.2', 'Tachicardia ventricolare'),
    ('ICD-10-IM', 'I49.0', 'Fibrillazione e flutter ventricolare'),
    ('ICD-10-IM', 'R00.1', 'Bradicardia, non specificata'),
    -- eventi che possono motivare l'impianto
    ('ICD-10-IM', 'R55',   'Sincope e collasso'),
    ('ICD-10-IM', 'R00.2', 'Palpitazioni'),
    ('ICD-10-IM', 'I46',   'Arresto cardiaco'),
    ('ICD-10-IM', 'I63',   'Infarto cerebrale (ictus ischemico)'),
    ('ICD-10-IM', 'G45',   'Attacco ischemico cerebrale transitorio (TIA)'),
    -- cardiopatie
    ('ICD-10-IM', 'I42.0', 'Cardiomiopatia dilatativa'),
    ('ICD-10-IM', 'I42.1', 'Cardiomiopatia ipertrofica ostruttiva'),
    ('ICD-10-IM', 'I42.2', 'Altre cardiomiopatie ipertrofiche (non ostruttive)'),
    ('ICD-10-IM', 'I50',   'Insufficienza cardiaca'),
    ('ICD-10-IM', 'I25',   'Cardiopatia ischemica cronica'),
    ('ICD-10-IM', 'I21',   'Infarto miocardico acuto'),
    ('ICD-10-IM', 'I35',   'Disturbi non reumatici della valvola aortica'),
    -- comorbidità frequenti
    ('ICD-10-IM', 'I10',   'Ipertensione essenziale'),
    ('ICD-10-IM', 'E11',   'Diabete mellito tipo 2'),
    ('ICD-10-IM', 'N18',   'Malattia renale cronica'),
    ('ICD-10-IM', 'J44',   'Broncopneumopatia cronica ostruttiva')
ON CONFLICT (coding_system, code) DO NOTHING;

INSERT INTO cied.explant_reason (code, label) VALUES
    ('BATTERY_EOL',       'Fine vita della batteria / sostituzione elettiva'),
    ('INFECTION',         'Infezione (tasca o sistemica)'),
    ('UPGRADE',           'Upgrade a un altro dispositivo'),
    ('MALFUNCTION',       'Malfunzionamento del dispositivo o dell''elettrocatetere'),
    ('RECALL',            'Richiamo / avviso del produttore'),
    ('DIAGNOSIS_REACHED', 'Diagnosi ottenuta (loop recorder)'),
    ('END_OF_MONITORING', 'Fine del periodo di monitoraggio senza diagnosi (loop recorder)'),
    ('DEATH',             'Decesso del paziente'),
    ('PATIENT_REQUEST',   'Richiesta del paziente'),
    ('OTHER',             'Altro (specificare nella nota)')
ON CONFLICT (code) DO NOTHING;

-- Produttori (MDC_IDC_ENUM_MFG). Aggiungere qui gli altri codici quando arrivano i
-- primi export (finché non c'è il nome, filtri e ricerca mostrano il codice).
INSERT INTO cied.manufacturer (code, name) VALUES
    ('BIO', 'Biotronik')
ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name;
