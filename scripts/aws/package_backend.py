#!/usr/bin/env python3
"""Build Lambda archives, upload them, and write a deployable stack template.

Example: python scripts/aws/package_backend.py --bucket ARTIFACT_BUCKET --prefix weather-station-dev/history --region us-east-1
Requires boto3 and PyYAML. Existing stack parameters/secrets are never read.
"""
import argparse
import hashlib
from pathlib import Path
import tempfile
import zipfile

import boto3
import yaml

class NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bucket', required=True)
    parser.add_argument('--prefix', default='weather-history')
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--profile')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    template = yaml.safe_load((root / 'aws/weather-station-stack.yaml').read_text())
    s3 = boto3.Session(profile_name=args.profile, region_name=args.region).client('s3')
    with tempfile.TemporaryDirectory(prefix='weather-packages-') as temporary:
        for kind, resources in [('ingest', ['IngestObservationFunction']), ('read', ['ReadObservationFunction']),
                                ('rollup', ['HistoryRollupFunction']), ('export', ['HistoryExportApiFunction', 'HistoryExportWorkerFunction'])]:
            sources = {'app.py': root / f'aws/{kind}/app.py'}
            if kind in ('read', 'rollup'):
                sources['aggregate_history.py'] = root / 'aws/rollup/aggregate_history.py'
            package = Path(temporary) / f'{kind}.zip'
            with zipfile.ZipFile(package, 'w') as archive:
                for name, source in sources.items():
                    entry = zipfile.ZipInfo(name, date_time=(2026, 10, 17, 0, 0, 0))
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(entry, source.read_bytes())
            digest = hashlib.sha256(package.read_bytes()).hexdigest()
            key = f'{args.prefix.strip("/")}/{digest}/{kind}.zip'
            s3.upload_file(str(package), args.bucket, key)
            for resource in resources:
                template['Resources'][resource]['Properties']['Code'] = {'S3Bucket': args.bucket, 'S3Key': key}
            print(f'Packaged {kind}: {digest}')
    output = root / 'aws/packaged-template.yaml'
    output.write_text(yaml.dump(template, Dumper=NoAliasDumper, sort_keys=False))
    print(f'Wrote {output}')


if __name__ == '__main__':
    main()
