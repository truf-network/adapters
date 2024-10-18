import os
import pandas as pd
from prefect import flow, task
from prefect.futures import wait, PrefectFuture
from examples.simple_gsheets.utils import filter_by_source_id, prepare_records_for_tsn
from tsn_adapters.tasks.github import read_repo_csv_file
from tsn_adapters.tasks.gsheet import read_gsheet
from tsn_adapters.tasks.tsn import insert_tsn_records, get_all_tsn_records
from tsn_adapters.tasks.data_manipulation import reconcile_data
import tsn_sdk.client as tsn_client

@flow(log_prints=True)
def gsheets_flow(repo: str, sources_path: str, destination_tsn_provider: str):
    """
    This flow ingests data from Google Sheets into TSN.
    It reads from a `primitive_sources.csv` file in the repo to know which sheets to ingest data from.

    It expects a CSV file in the repo with the following columns:
    - source_type: the type of source, e.g. gsheets:<gsheets_id>
    - stream_id: the TSN stream_id to insert the records into
    - source_id: the identification to filter the records by on the source

    Example publicly available at https://docs.google.com/spreadsheets/d/1WE3Sw_ZZ4IyJmcqG5BTTtAMX6qRX0_k8dBlnH2se7dI/view

    Expects from gsheet:
    - Year: YYYY
    - Month: MM
    - ID: the identification to filter the records by on the source
    - Value: the value to insert into TSN
    
    It will fetch records from all the sources and insert them into TSN, creating the stream if needed.
    """


    # Read the sources from the CSV file in the repo
    sources_df = read_repo_csv_file(repo, sources_path)
    print(f"Found {len(sources_df)} sources to be ingested")

    # we want to know from which sources we are ingesting data
    # get unique source_types to extract the gsheets_ids
    source_types = sources_df["source_type"].unique().tolist()
    print(f"Found {len(source_types)} source types: {source_types}")

    # extract the gsheets_id from the source_type, ensuring it starts with 'gsheets'
    gsheets_ids = [
        source_type.split(":")[1]
        for source_type in source_types
        if source_type.startswith("gsheets:")
    ]
    print(f"Found {len(gsheets_ids)} gsheets ids: {gsheets_ids}")

    # store insertion tasks
    insert_jobs = []

    # initialize the TSN client
    client = tsn_client.TSNClient(destination_tsn_provider, token=os.environ["TSN_PRIVATE_KEY"])

    for gsheets_id in gsheets_ids:

        # Fetch the records from the sheet
        print(f"Fetching records from sheet {gsheets_id}")
        records = (gsheets_id)

        # for each source, fetch the records and transform until we can insert them into TSN
        # insertions happen concurrently
        for _, row in sources_df.iterrows():
            # deploy the source_id if needed
            deployment_job = deploy_primitive_if_needed.submit(row["stream_id"], client)

            # Normalize the records
            normalized_records = normalize_source(records)

            # Filter the records by source_id
            filtered_records = filter_by_source_id(normalized_records, row["source_id"])
            print(f"Found {len(filtered_records)} records for {row['source_id']}")

            prepared_records = prepare_records_for_tsn(filtered_records)

            # Get the existing records from TSN
            existing_records = get_all_tsn_records.submit(row["stream_id"], client, wait_for=[deployment_job])

            # Reconcile the records with the existing ones in TSN, to push only new records
            reconciled_records = reconcile_data.submit(existing_records, prepared_records)

            # Insert the records into TSN, concurrently
            insert_job = insert_tsn_records.submit(row["stream_id"], reconciled_records, client)
            insert_jobs.append(insert_job)

    # Wait for all the insertions to complete
    wait(insert_jobs)

@task
def deploy_primitive_if_needed(stream_id: str, client: tsn_client.TSNClient):
    # check if the stream_id exists in the TSN 
    stream_exists = client.stream_exists(stream_id)
    if not stream_exists:
        print(f"Stream {stream_id} does not exist, deploying...")
        client.deploy_stream(stream_id, wait=True)
        print(f"Stream {stream_id} deployed. Initializing...")
        client.init_stream(stream_id, wait=True)
        print(f"Stream {stream_id} initialized.")
    else:
        print(f"Stream {stream_id} already exists.")

@task
def normalize_source(df: pd.DataFrame) -> pd.DataFrame:
    inputs = df.copy()
    # add default day
    inputs['Day'] = 1

    # Combine Year, Month, and Day into a string and then convert to datetime
    inputs['date'] = pd.to_datetime(inputs['Year'].astype(str) + ' ' + inputs['Month'] + ' ' + inputs['Day'].astype(str))
    inputs["value"] = inputs["Value"].astype(float)

    # rename columns to match the expected ones
    inputs = inputs.rename({"ID": "source_id"}, axis=1)

    # select the columns we need
    inputs = inputs[["date", "value", "source_id"]]

    return inputs


if __name__ == "__main__":
    repo = "truflation/tsn-adapters"
    repo_sources_path = "examples/simple_gsheets/primitive_sources.csv"
    destination_tsn_provider = os.environ["TSN_PROVIDER"]

    gsheets_flow(repo, repo_sources_path, destination_tsn_provider)
