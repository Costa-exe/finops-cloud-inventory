"""Generate an offline PDF coverage guide using only the Python standard library."""
import argparse
import os
from pathlib import Path
import tempfile
import textwrap

from aws_inventory.service_catalog import SERVICES, PRICED_SERVICES


class Document:
    """Small text-only A4 PDF writer; embedded content has no scripts or network assets."""
    def __init__(self):
        self.pages = []
        self.commands = []
        self.y = 780

    def text(self, text, *, size=10, bold=False, color="0.12 0.19 0.26", x=48):
        text = text.replace("→", " -> ").replace("–", "-").replace("—", "-")
        encoded = text.encode("cp1252", errors="replace").hex()
        self.commands.append(f"BT /{'F2' if bold else 'F1'} {size} Tf {color} rg {x} {self.y} Td <{encoded}> Tj ET")
        self.y -= size*1.45

    def paragraph(self, text, *, size=10, bold=False, gap=6):
        width = max(35, int(480/(size*.53)))
        lines = textwrap.wrap(text, width=width, break_long_words=True, break_on_hyphens=False) or [""]
        for line in lines:
            if self.y < 65:
                self.new_page()
            self.text(line, size=size, bold=bold)
        self.y -= gap

    def new_page(self):
        if self.commands:
            self.pages.append("\n".join(self.commands))
        self.commands = []
        self.y = 795
        self.text("CLOUD INVENTORY  /  COPERTURA E METODO", size=9, bold=True, color="0 0.40 0.38")
        self.y -= 15

    def output(self):
        if self.commands:
            self.pages.append("\n".join(self.commands))
            self.commands = []
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"]
        kids = []
        for index, page in enumerate(self.pages):
            page_id, stream_id = len(objects)+1, len(objects)+2
            kids.append(f"{page_id} 0 R")
            footer = f"BT /F1 9 Tf 0.4 0.45 0.5 rg 48 32 Td (Cloud Inventory | {index+1} / {len(self.pages)}) Tj ET"
            stream = (page+"\n"+footer).encode("ascii")
            objects += [f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.28 841.89] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {stream_id} 0 R >>".encode(), b"<< /Length "+str(len(stream)).encode()+b" >>\nstream\n"+stream+b"\nendstream"]
        objects[1] = f"<< /Type /Pages /Count {len(kids)} /Kids [{' '.join(kids)}] >>".encode()
        result = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for index, value in enumerate(objects, 1):
            offsets.append(len(result))
            result.extend(f"{index} 0 obj\n".encode()+value+b"\nendobj\n")
        xref = len(result)
        result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
        result.extend("".join(f"{offset:010d} 00000 n \n" for offset in offsets[1:]).encode())
        result.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
        return bytes(result)


def build_guide():
    doc = Document()
    doc.new_page()
    doc.paragraph("Servizi, relazioni e criteri di costo", size=25, bold=True, gap=15)
    doc.paragraph("Guida alla copertura implementata - 35 famiglie di servizi", size=14, bold=True)
    doc.paragraph("Questa guida descrive il codice incluso nel progetto. Non dichiara una copertura universale di AWS: ogni famiglia ha un perimetro esplicito e limiti elencati. Aurora rientra in RDS; Fargate in ECS/EKS; VPN e Transit Gateway in VPC.")
    doc.paragraph("Tre informazioni da non confondere", size=15, bold=True)
    for text in (
        "FATTURATO: importi restituiti da Cost Explorer, mese per mese, raggruppati per servizio e regione. Questi totali non vengono ripartiti arbitrariamente sulle risorse.",
        "STIMATO: tariffe attuali di AWS Price List Query API abbinate a configurazione e quantità note. Si usano solo corrispondenze univoche; un catalogo ambiguo lascia la componente non disponibile.",
        "NON DISPONIBILE: servono consumi, quantità, configurazioni o abbinamenti non raccolti. Non equivale a zero e non viene sostituito da una stima inventata.",
        "Per risorsa, la fatturazione diretta attualmente riguarda EC2 e richiede l'opzione --resource-costs e i requisiti AWS. Le altre famiglie hanno stime supportate o soli totali di servizio.",
    ):
        doc.paragraph(text)
    doc.paragraph("Periodo, valuta e dipendenze", size=15, bold=True)
    doc.paragraph("Il periodo è UTC: inizio incluso, fine esclusa. L'inventario è attuale. Le stime sui mesi passati applicano la configurazione attuale a quegli intervalli, senza ricostruire accensioni, spegnimenti o risorse eliminate.")
    doc.paragraph("L'HTML mostra solo USD, senza conversioni: eventuali importi in altre valute sono esclusi con un avviso. --language en|it traduce l'HTML; --date-format iso|dmy|mdy cambia la visualizzazione. --timezone Europe/Rome imposta il fuso orario mostrato, anche tramite prompt. Gli argomenti --from/--to restano YYYY-MM-DD e i periodi di fatturazione restano UTC.")
    doc.paragraph("Una dipendenza inclusa contribuisce al subtotale del genitore. Un riferimento (VPC, destinazione evento, chiave KMS) viene mostrato senza attribuire automaticamente costi. I subtotali possono sovrapporsi; nei totali generali ogni costo proprio è contato una sola volta.")
    doc.new_page()
    doc.paragraph("Raggruppamenti e formule", size=23, bold=True)
    for title, explanation in (
        ("Mese / servizio / regione", "Cost Explorer restituisce la metrica scelta, per default AmortizedCost. Il grafico di fatturazione è separato dal modello delle risorse. EC2 Other contiene più componenti: il filtro EBS o VPC non trasforma il totale condiviso in un'attribuzione esatta."),
        ("Mese / risorsa e configurazione", "Tariffa istanza-ora x quantità x ore del periodo x ore/giorno ipotizzate / 24. EC2 e RDS arrestati assumono compute nullo nello scenario. Per cluster gestiti si usa il numero di nodi quando noto."),
        ("Mese / volume", "GiB allocati x tariffa GiB-mese x quota del mese selezionata; extra gp3 IOPS oltre 3.000 e throughput oltre 125 MiB/s solo se prezzabili. Lo storage non si spegne insieme alla macchina."),
        ("Mese / rete e bilanciatori", "Quota oraria base x ore del periodo; endpoint Interface x numero di subnet/AZ. Traffico, LCU e altre componenti variabili non si deducono dall'inventario. I NAT regionali/varianti non rappresentati in modo univoco devono restare non prezzati."),
        ("Mese / cluster e dipendenze", "EKS: control plane con supporto esplicito + EC2 + EBS. ECS: task Fargate supportati e/o EC2 + EBS. RDS: cluster + istanze note. ALB: base + target risolti. I grafici per servizio mostrano costi propri; le schede mostrano anche il subtotale con dipendenze."),
        ("Solo spesa del servizio", "Per i servizi a consumo senza dati di utilizzo il report conserva l'inventario, i riferimenti e la spesa mensile del servizio. La tabella seguente elenca le unità rilevanti, non promette che siano tutte applicate dal motore di stima."),
    ):
        doc.paragraph(title, size=12, bold=True)
        doc.paragraph(explanation)
    doc.paragraph("Prezzi aggiornati", size=15, bold=True)
    doc.paragraph("Nessun prezzo reale è codificato nel programma. Il catalogo viene interrogato durante l'esportazione, con cache per configurazione e paginazione. SKU, valuta e data di efficacia restano nei dati. Disponibilità del catalogo e permessi possono impedire il matching. I prezzi della simulazione sono fittizi.")
    for index, (selector, record) in enumerate(sorted(SERVICES.items()), 1):
        title, entities, relations, rates, estimate, limitations = record
        # One service per page keeps dense descriptions readable and reliably paginated.
        doc.new_page()
        doc.paragraph(f"{index:02d}  {title}", size=24, bold=True, gap=14)
        doc.paragraph("Selettore: --service "+selector, size=11, bold=True)
        doc.paragraph("Stime supportate (parziali)" if selector in PRICED_SERVICES else "Inventario + spesa di servizio; eventuali dipendenze note", size=12, bold=True, gap=20)
        for label, body in (("Inventario raccolto", entities), ("Relazioni ricostruite", relations),
                            ("Tariffe e raggruppamenti rilevanti", rates), ("Calcolo effettivamente implementato", estimate), ("Limiti da considerare", limitations)):
            doc.paragraph(label, size=13, bold=True)
            doc.paragraph(body, size=11, gap=17)
        doc.paragraph("L'HTML mostra una sezione alla volta, con filtri per mese, servizio e regione e importi in USD. Da Esportazioni si scarica un Excel per servizio: spesa, tipi, risorse e costi associati, con mesi, totali e colonna Regione, senza menu a tendina. Le relazioni non risolte restano identificativi; non costituiscono una prova di attribuzione economica.", size=9)
    doc.new_page()
    doc.paragraph("Simulazione e riferimenti", size=23, bold=True)
    doc.paragraph("Scenario: tutte le 35 famiglie, risorse regionali in eu-west-1, eu-central-1 ed eu-north-1. CloudFront e Route 53 sono globali; S3 ha un bucket per regione. Periodo 01/07/2026 - 01/10/2026 (fine esclusa). Tutti i dati e prezzi sono fittizi.")
    doc.paragraph("Le risposte SDK sono validate da botocore Stubber. La simulazione esegue gli stessi collector e lo stesso renderer dell'esportazione reale, con DNS, socket e HTTP bloccati. Nessuna credenziale reale, AWS CLI o chiamata AWS è necessaria.")
    doc.paragraph("Rigenerazione", size=14, bold=True)
    doc.paragraph("python -m examples.simulate_export --service all --language it --date-format dmy --timezone Europe/Rome --overwrite", size=10)
    doc.paragraph("python -m examples.generate_service_guide --overwrite", size=10)
    doc.paragraph("Fonti tecniche ufficiali", size=14, bold=True)
    for url in (
        "https://docs.aws.amazon.com/boto3/latest/reference/services/",
        "https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/using-price-list-query-api.html",
        "https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsage.html",
        "https://aws.amazon.com/vpc/pricing/", "https://aws.amazon.com/privatelink/pricing/",
        "https://aws.amazon.com/elasticloadbalancing/pricing/", "https://aws.amazon.com/fargate/pricing/",
        "https://aws.amazon.com/ebs/pricing/", "https://aws.amazon.com/rds/pricing/",
    ):
        doc.paragraph(url, size=9)
    return doc.output()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/aws_service_coverage.pdf"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    path = args.output.absolute()
    if path.is_symlink() or path.is_dir() or path.suffix.lower() != ".pdf":
        parser.error("Output must be a regular PDF file path.")
    if path.exists() and not args.overwrite:
        parser.error("Output exists; use --overwrite.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(build_guide())
            stream.flush()
            os.fsync(stream.fileno())
        if args.overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
            temporary.unlink()
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(path)


if __name__ == "__main__":
    main()
