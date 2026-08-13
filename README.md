# Gestion des factures

Application Streamlit qui découpe un PDF de factures groupées, identifie le
client de chaque facture, et permet de les rechercher, de suivre leur envoi et
de les exporter renommées au nom du client.

## Ce que fait l'application

1. **Import** — vous choisissez le mois traité, puis déposez un ou plusieurs
   PDF. Un même fichier peut contenir plusieurs centaines de factures.
2. **Extraction** — pour chaque facture : nom du client, numéro, date, ICE,
   total HT, TVA et TTC.
3. **Suivi** — un tableau avec recherche plein texte, filtre par mois, filtre
   d'envoi, et une case « Envoyé au client » par ligne.
4. **Export** — téléchargement unitaire (`NOM DU CLIENT.pdf`) ou archive ZIP
   d'un mois complet.

### Structure de l'archive ZIP

Un client avec une seule facture est placé à la racine ; un client avec
plusieurs factures obtient un dossier à son nom.

```
factures-2026-07.zip
├── ABM INVEST.pdf
├── ABS CUSTOM RIDES.pdf
├── VELVETRAV/
│   ├── VELVETRAV - 1145-2026.pdf
│   ├── VELVETRAV - 1175-2026.pdf
│   └── VELVETRAV - 1185-2026.pdf
└── Hôtel Nord Pinus Tanger/
    ├── Hôtel Nord Pinus Tanger - 1104-2026.pdf
    └── Hôtel Nord Pinus Tanger - 1160-2026.pdf
```

## Installation locale

```bash
pip install -r requirements.txt
```

```bash
streamlit run app.py
```

L'application s'ouvre sur <http://localhost:8501>.

## Déploiement sur Streamlit Community Cloud

1. Poussez ce dépôt sur GitHub.
2. Sur <https://share.streamlit.io>, créez une application pointant sur ce
   dépôt, branche `main`, fichier principal `app.py`.

Les dépendances de `requirements.txt` et les réglages de
`.streamlit/config.toml` (limite d'envoi portée à 500 Mo) sont pris en compte
automatiquement.

> **Persistance des statuts « Envoyé ».** Le système de fichiers de Streamlit
> Community Cloud est éphémère : les cases cochées sont perdues au redémarrage
> du serveur. L'application le signale dans la barre latérale. Pour un suivi
> durable, hébergez l'application sur une machine disposant d'un disque
> persistant et pointez `FACTURES_DB_PATH` vers ce disque.

## Comment le nom du client est identifié

Le gabarit place le bloc destinataire à une position fixe : colonne de droite
(x ≈ 312 pt), première ligne sous la date d'émission. L'extraction s'appuie sur
ces coordonnées plutôt que sur le texte brut, ce qui évite de confondre le nom
avec une ligne d'adresse.

Un nom réparti sur deux lignes est recollé : dans le gabarit, un simple retour
à la ligne vaut 16,6 pt tandis qu'une ligne vide sépare le nom de l'adresse par
33,1 pt. Le seuil de distinction est fixé à mi-chemin.

```
Facture N° : 1075 - 2026              Tanger le, 31/07/2026
ICE : 003070399000083
                                      CONSEIL EN INVESTISSEMENT   <- nom
                                      IMMOBILIER ET INDUSTRIEL    <- suite du nom
                                                                  (ligne vide)
                                      Chez EASYDOM BUSINESS...    <- adresse
```

Ces repères sont regroupés dans `LayoutProfile`
([extraction.py](src/factures/extraction.py)) : si le gabarit évolue, seules
ces constantes sont à ajuster.

## Fiabilité

* **Aucune facture perdue.** Une page illisible est isolée et signalée dans le
  rapport d'anomalies ; elle n'interrompt jamais le traitement du lot.
* **Aucun nom deviné.** Si le nom du client est introuvable, la facture est
  signalée en erreur plutôt que nommée approximativement. Un panneau
  « Corriger un nom de client » permet de rectifier avant export.
* **Contrôles automatiques.** Cohérence HT + TVA = TTC, date lisible, numéro
  présent — chaque écart produit un avertissement visible.
* **Noms de fichiers sûrs.** Caractères interdits par Windows, noms réservés
  (`CON`, `LPT1`…), longueurs excessives et collisions sont traités.
* **Statuts stables.** La case « Envoyé » est indexée sur une empreinte du PDF
  source : réimporter le même fichier retrouve les cases déjà cochées.

## Développement

```bash
pip install -e ".[dev]"
```

```bash
python -m pytest -q --cov=factures
```

```bash
python -m ruff check .
```

### Organisation

```
app.py                      Interface Streamlit (présentation uniquement)
src/factures/
├── models.py               Invoice, ExtractionReport, périodes
├── extraction.py           Analyse de la mise en page des PDF
├── naming.py               Noms de fichiers sûrs et uniques
├── packaging.py            Découpe des PDF et archives ZIP
└── storage.py              Persistance SQLite du statut « envoyé »
tests/                      110 tests (extraction, nommage, ZIP, base, UI)
```

Les tests fabriquent leurs propres PDF aux coordonnées du gabarit
([tests/conftest.py](tests/conftest.py)) : **aucune facture réelle n'est
versionnée**. Le `.gitignore` exclut d'ailleurs tout `*.pdf` et toute base de
données.

## Configuration

| Variable | Rôle | Défaut |
| --- | --- | --- |
| `FACTURES_DB_PATH` | Emplacement de la base de suivi des envois | `data/factures.db` |
