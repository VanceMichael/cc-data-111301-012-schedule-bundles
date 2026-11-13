import csv
import io
from pathlib import Path
import zipfile


def inspect_file(path):
    files = []
    with zipfile.ZipFile(Path(path)) as archive:
        for name in archive.namelist():
            if name.endswith('.txt'):
                with archive.open(name) as stream, io.TextIOWrapper(stream,encoding='utf-8-sig',newline='') as reader:
                    rows = csv.reader(reader)
                    header = next(rows, [])
                    files.append({'name':name,'columns':header,'rows':sum(1 for row in rows)})
    return {'files':files}
