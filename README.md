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

## Persistance des statuts « Envoyé »

L'application choisit son stockage automatiquement, et affiche lequel est
utilisé en bas de la barre latérale.

| Stockage | Quand | Durabilité |
| --- | --- | --- |
| **Postgres** (Neon, Supabase, autre) | Dès qu'une chaîne de connexion est configurée | Survit à tout redémarrage |
| **SQLite** | Sinon | Suffisant en local, perdu sur un hébergement éphémère |

Le disque de Streamlit Community Cloud étant éphémère, **Postgres est
indispensable pour un déploiement en ligne** : sans lui, les cases cochées
disparaissent à chaque redémarrage du serveur.

### Quel fournisseur Postgres ?

L'application n'est liée à aucun hébergeur : **toute base Postgres convient**,
seule la chaîne de connexion change. Le critère décisif pour un usage mensuel
est le comportement en cas d'inactivité.

| Fournisseur | Gratuit | Inactivité |
| --- | --- | --- |
| **Neon** | 0,5 Go | Se met en veille après quelques minutes mais **reprend seule en moins d'une seconde**, sans intervention |
| **Supabase** | 500 Mo | Projet **suspendu après ~7 jours**, à réactiver à la main depuis le tableau de bord |

Pour une application ouverte une fois par mois, **Neon est le choix le plus
sûr** : rien à réveiller, rien à surveiller. Supabase reste parfaitement
utilisable si son projet est maintenu actif (voir plus bas).

Ces politiques évoluent : vérifiez-les au moment de créer le projet.

### Configurer la base

1. Récupérez la chaîne de connexion :
   * **Neon** — tableau de bord du projet, encadré *Connection string*.
     Conservez le paramètre `?sslmode=require`.
   * **Supabase** — bouton **Connect** en haut, section *Connection string*,
     mode **Transaction pooler** (port 6543, mieux adapté aux connexions
     courtes d'une application web que le port 5432). Remplacez
     `[YOUR-PASSWORD]` par le mot de passe de la base, réinitialisable dans
     *Settings > Database > Reset database password*.
2. **En local** : copiez `.streamlit/secrets.toml.example` en
   `.streamlit/secrets.toml` et collez-y la chaîne. Ce fichier est exclu du
   dépôt par `.gitignore`.
4. **Sur Streamlit Cloud** : collez le même contenu dans *Settings > Secrets*.

La table `facture_envois` est créée automatiquement au premier démarrage.
Aucune autre table n'est touchée.

Alternative sans fichier de secrets : définir la variable d'environnement
`FACTURES_POSTGRES_URL` (ou `SUPABASE_DB_URL`).

### Éviter les mises en veille

Deux hébergements gratuits s'endorment après ~7 jours sans activité, ce qui
piège une application utilisée une fois par mois : le projet **Supabase** (à
réactiver manuellement) et l'app **Streamlit Community Cloud** (à réveiller
d'un clic, ~30 s). Le workflow
[`.github/workflows/keep-alive.yml`](.github/workflows/keep-alive.yml) les
sollicite tous les 3 jours.

Ses deux étapes sont indépendantes et facultatives ; chacune est simplement
ignorée si son paramètre est absent. Dans *Settings > Secrets and variables >
Actions* :

| Paramètre | Type | Rôle |
| --- | --- | --- |
| `FACTURES_POSTGRES_URL` | Secret | Maintient la base active. **Inutile avec Neon**, qui reprend seule. |
| `STREAMLIT_APP_URL` | Variable | Maintient l'application éveillée. |

L'app endormie ne fait perdre aucune donnée : tout est dans la base. Ce n'est
qu'un confort.

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
├── backend.py              Contrat commun et choix du stockage
├── storage.py              Backend SQLite
└── postgres_storage.py     Backend Postgres / Supabase
tests/                      129 tests (extraction, nommage, ZIP, stockage, UI)
```

Les tests fabriquent leurs propres PDF aux coordonnées du gabarit
([tests/conftest.py](tests/conftest.py)) : **aucune facture réelle n'est
versionnée**. Le `.gitignore` exclut d'ailleurs tout `*.pdf` et toute base de
données.

## Configuration

| Variable | Rôle | Défaut |
| --- | --- | --- |
| `FACTURES_POSTGRES_URL` | Chaîne de connexion Postgres. Sa présence active le backend Postgres. | — |
| `SUPABASE_DB_URL` | Alias accepté pour la précédente | — |
| `FACTURES_DB_PATH` | Emplacement de la base SQLite, quand Postgres n'est pas configuré | `data/factures.db` |
| `FACTURES_TEST_POSTGRES_URL` | Base contre laquelle exécuter les tests d'intégration Postgres | — |

Aucun secret n'est versionné : `.gitignore` exclut `.streamlit/secrets.toml`.
Les mots de passe sont masqués dans les journaux et dans l'interface.
