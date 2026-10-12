"""Display translations; identifiers, source currencies and AWS messages stay intact."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .translations import LABELS as FRENCH_SPANISH, MESSAGES, TEMPLATES

LANGUAGES = ("en", "it", "fr", "es")
LOCALES = {"en": "en-US", "it": "it-IT", "fr": "fr-FR", "es": "es-ES"}


LABELS = {
    "sections": ("Report sections", "Sezioni del report"),
    "title": ("AWS resource costs", "Costi delle risorse AWS"),
    "overview": ("Overview", "Panoramica"), "services": ("Services", "Servizi"),
    "resources": ("Resources and attached costs", "Risorse e costi associati"),
    "billing": ("AWS service billing", "Spesa AWS per servizio"),
    "month": ("Month", "Mese"),
    "search_label": ("Search resources", "Cerca risorse"),
    "non_usd": ("Non-USD source amounts are excluded from this USD report; no conversion is performed.", "Gli importi della fonte diversi da USD sono esclusi da questo report in dollari; non vengono convertiti."),
    "region": ("Region", "Regione"), "service": ("Service", "Servizio"),
    "all_regions": ("All regions", "Tutte le regioni"), "all_services": ("All services", "Tutti i servizi"),
    "theme": ("Light / dark", "Chiaro / scuro"), "account": ("Account", "Account"),
    "model": ("Known own-resource costs", "Costi propri delle risorse noti"),
    "estimated": ("Of which estimated", "Di cui stimati"),
    "bill_total": ("Service billing • separate total", "Spesa dei servizi • totale separato"),
    "model_note": ("Each own cost counted once within the selected service and region.", "Ogni costo proprio contato una volta nel servizio e nella regione selezionati."),
    "estimate_note": ("Included in the model. Current capacity at current prices, not historical usage.", "Inclusi nel modello. Capacità attuale ai prezzi attuali, non utilizzo storico."),
    "billing_note": ("Cost Explorer amounts are not added to resource estimates.", "Gli importi di Cost Explorer non si sommano alle stime delle risorse."),
    "trend": ("Monthly costs", "Costi mese per mese"),
    "chart_services": ("Own costs by service", "Costi propri per servizio"),
    "chart_resources": ("Own costs by resource • top 10", "Costi propri per risorsa • primi 10"),
    "chart_regions": ("Own costs by region", "Costi propri per regione"),
    "search": ("Search name, ID, type or cluster", "Cerca nome, ID, tipo o cluster"),
    "unavailable": ("Unavailable", "Non disponibile"),
    "billed": ("Billed", "Fatturato"), "mixed": ("Mixed", "Misto"),
    "estimated_basis": ("Estimated", "Stimato"),
    "partial": ("Known subtotal / incomplete", "Subtotale noto / incompleto"),
    "own": ("Own monthly cost", "Costo mensile proprio"),
    "attached": ("Resource + attached costs", "Risorsa + costi associati"),
    "attachments": ("Attached resource costs", "Costi delle risorse associate"),
    "relations": ("Resource links", "Collegamenti tra risorse"),
    "relations_note": ("Links reported by AWS, such as an instance attached to a disk or belonging to a VPC. Only links marked as included contribute to the subtotal.", "Collegamenti comunicati da AWS, per esempio un disco collegato a un'istanza o l'appartenenza a una VPC. Solo quelli indicati come inclusi contribuiscono al subtotale."),
    "export_resources": ("Download Excel · 4 sheets", "Scarica Excel · 4 fogli"),
    "exports": ("Downloads", "Esportazioni"),
    "by_type": ("By instance / resource type", "Per tipo di istanza / risorsa"),
    "by_resource": ("By resource", "Per risorsa"),
    "sheet_billing": ("Monthly service billing and period total, separate from estimates.", "Spesa mensile del servizio e totale periodo, separati dalle stime."),
    "sheet_type": ("Own costs by instance, disk or resource type.", "Costi propri per tipo di istanza, disco o risorsa."),
    "sheet_resource": ("Resource name, region, catalog rate and own costs for each month.", "Nome risorsa, regione, tariffa e costi propri per ogni mese."),
    "sheet_attached": ("Resource plus attachments: monthly subtotal and a short note on included services. Shared costs counted once in totals.", "Risorsa più annesse: subtotali mensili e una nota sui servizi inclusi. Costi condivisi contati una volta nei totali."),
    "select_resource": ("Choose a resource", "Scegli una risorsa"),
    "type": ("Type", "Tipo"),
    "included_in_parent": ("This resource is included in the linked parent's subtotal", "Questa risorsa è inclusa nel subtotale della risorsa collegata"),
    "export_note": ("One Excel file per service, with all report months and regions, monthly totals and period totals.", "Un Excel per servizio, con tutti i mesi e le regioni del report, totali mensili e totale del periodo."),
    "export_done": ("Download requested", "Download richiesto"),
    "export_error": ("Download failed. Try again in a browser that allows local file downloads.", "Download non riuscito. Riprova in un browser che consenta di scaricare file locali."),
    "reference": ("Reference only / not added to costs", "Solo riferimento / non sommato ai costi"),
    "included": ("Included in subtotal", "Incluso nel subtotale"),
    "calculation": ("Calculation paradigm", "Paradigma di calcolo"),
    "component": ("Component", "Componente"), "rate": ("Catalog rate", "Tariffa di catalogo"),
    "source": ("Source", "Fonte"),
    "resource": ("Resource", "Risorsa"), "reason": ("Why no amount", "Perché manca l'importo"),
    "metric": ("Metric", "Metrica"), "aws_cost": ("AWS monthly amount", "Importo AWS mensile"),
    "empty": ("No resources match the selected filters.", "Nessuna risorsa corrisponde ai filtri selezionati."),
    "no_billing": ("No billing in this scope and source currency.", "Nessun importo di fatturazione per questa selezione e valuta."),
    "simulation": ("OFFLINE SIMULATION — all resources, billing and prices are fictional.", "SIMULAZIONE OFFLINE — risorse, fatturazione e prezzi sono tutti fittizi."),
    "read_errors": ("PARTIAL EXTRACTION — some AWS reads failed.", "ESTRAZIONE PARZIALE — alcune letture AWS non sono riuscite."),
    "shared_billing": ("EC2 Other is a shared billing category: its total is not an exact EBS or VPC allocation.", "EC2 Other è una categoria condivisa: il totale non è un'attribuzione esatta a EBS o VPC."),
    "footer": ("Offline file · Read-only SDK inventory · AWS Cost Explorer · AWS Price List Query API", "File offline · Inventario SDK in sola lettura · AWS Cost Explorer · AWS Price List Query API"),
    "no_js": ("Enable JavaScript for filters and charts. Latest-month subtotals:", "Abilita JavaScript per filtri e grafici. Subtotali dell'ultimo mese:"),
}


def labels(language):
    index = LANGUAGES.index(language)
    words = {key: (*pair, *FRENCH_SPANISH[key])[index] for key, pair in LABELS.items()}
    words.update({key: values[index] for key, values in TEMPLATES.items()})
    return words


def translated_messages(language):
    if language not in {"fr", "es"}:
        return {}
    index = 0 if language == "fr" else 1
    return {key: values[index] for key, values in MESSAGES.items()}


def translate_message(english, italian, language):
    if language == "it":
        return italian
    if language in {"fr", "es"}:
        return MESSAGES[english][0 if language == "fr" else 1]
    return english


def display_date(value, style="iso"):
    date = str(value)[:10]
    if len(date) == 10 and date[4] == "-" and date[7] == "-":
        year, month, day = date.split("-")
        return f"{day}/{month}/{year}" if style == "dmy" else f"{month}/{day}/{year}" if style == "mdy" else date
    return date


def validate_timezone(value):
    """Reject invalid IANA zones before making any AWS requests."""
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("Unknown timezone. Use an IANA name such as Europe/Rome, America/New_York or UTC; install dependencies from requirements.txt (including tzdata).") from exc


def display_datetime(value, style="iso", zone="UTC"):
    instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    local = instant.astimezone(ZoneInfo(zone))
    return f"{display_date(local.isoformat(), style)} {local:%H:%M} {zone} ({local:%z})"
