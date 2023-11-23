from argparse import ArgumentParser
import json
from pathlib import Path
from pprint import pprint
from urllib.parse import urlparse

from datalad_tabby.io import load_tabby
from datalad.api import catalog_add, catalog_remove, catalog_set, catalog_validate
from datalad_next.datasets import Dataset
from datalad_next.exceptions import IncompleteResultsError
from datalad_next.utils import get_dataset_root
from datalad_catalog.schema_utils import (
    get_metadata_item,
)

from pyld import jsonld

from queries import (
    process_ols_term,
    repr_ncbitaxon,
    repr_uberon,
)
from utils import mint_dataset_id


def process_authors(authors):
    """Convert author(s) to a list of catalog-schema authors"""
    known_keys = [
        "name",
        "email",
        "identifiers",
        "givenName",
        "familyName",
        "honorificSuffix",
    ]

    if authors is None:
        return None
    if isinstance(authors, dict):
        authors = [authors]

    result = []
    for author in authors:
        # drop not-known keys (like @type)
        d = {k: v for k, v in author.items() if k in known_keys and v is not None}
        # re-insert orcid as identifiers
        if orcid := author.get("orcid", False):
            d["identifiers"] = [
                {"name": "ORCID", "identifier": orcid},
            ]
        result.append(d)

    return result


def process_license(license):
    """Convert license to catalog-schema object

    Catalog schema expects name & url. We can reasonably expect
    schema:license to be a URL, but won't hurt checking. But what
    about the name?

    """
    if license is None:
        return None
    parsed_url = urlparse(license)
    if parsed_url.scheme != "" and parsed_url.netloc != "":
        # looks like a URL
        pass

    # do the least work, for now
    return {"name": license, "url": license}


def process_publications(publications):
    """Convert publication to catalog-schema object

    Catalog schema expects title, doi, datePublished,
    publicationOutlet, & authors. Our tabby spec only makes "citation"
    required, and allows doi, url, and datePublished (if doi is given,
    citation becomes optional).

    Best thing we can do in the citation-only case is to squeeze
    citation into title (big text that's displayed).

    When DOI is given, we can look it up to get all fields, and it's
    our artistic license whether citation should take precedence or
    not.

    """
    if publications is None:
        return None
    if type(publications) is dict:
        publications = [publications]

    res = []
    for publication in publications:
        citation = publication.pop("citation", None)

        if citation is not None:
            publication["title"] = citation
            publication["authors"] = []

        # todo: doi lookup
        res.append(publication)

    return res


def process_funding(funding):
    """Edit funding item(s), return an array

    Ensure that funding is an array. Edit DFG funding to spell out
    Deutsche Forschungsgemeinschaft and build a gepris url from the
    identifier. For SFB1451, populate additional fields based on the
    identifier (assuming 431549029-<project> format).

    Each funding element will have a @type: https://schema.org/Grant,
    so that we can apply custom rules in catalog template.

    """

    if isinstance(funding, dict):
        funding = [funding]

    grant_list = []
    for f in funding:
        grant = f.copy()
        if grant.get("funder") == "DFG":
            grant["funder"] = "Deutsche Forschungsgemeinschaft (DFG)"
            if grant.get("identifier").startswith("431549029-"):
                project, _, subproject = grant.get("identifier").rpartition("-")
                grant["name"] = "SFB 1451: Key mechanisms of motor control in health and disease"
                grant["alternateName"] = f"SFB 1451 ({project}, {subproject} project)"
                grant["identifier"] = f"https://gepris.dfg.de/gepris/projekt/{project}"
            else:
                grant["identifier"] = f"https://gepris.dfg.de/gepris/projekt/{grant.get(identifier)}"

        # replace compact IRI with a full IRI
        grant["@type"] = grant.get("@type", "").replace("schema:", "https://schema.org/")

        grant_list.append(grant)

    return grant_list

def process_keywords(keywords):
    """Ensure that keywords are an array"""
    return [keywords] if isinstance(keywords, str) else keywords


def process_arc(data_controller):
    """Convert data controller to access request contact

    Since there can only be one, uses the first data
    controller. Splits name (very naively) to satisfy catalog
    schema. Oblivious to the fact that the controller needs not be a
    person.

    """

    if data_controller is None:
        return None
    if isinstance(data_controller, list):
        data_controller = data_controller[0]

    first, _, last = data_controller.get("name", "").rpartition(" ")
    email = data_controller.get("email", "")

    return {"givenName": first, "familyName": last, "email": email}


def process_data_controller(data_controller):
    """Convert data controller to a dict or list of dict

    Adds schema.org Person type (which means we assume all data
    controllers to be persons) for linked data scenarios.

    """

    if data_controller is None:
        return None
    if isinstance(data_controller, list):
        return [process_data_controller(dc) for dc in data_controller]

    return {"@type": "https://schema.org/Person"} | data_controller


def process_used_for(activity):
    """Change an activity-dict to a schema.org Thing

    The activity coming from SFB1451-tabby has a title, description,
    and URL. We will use a generic schema.org Thing to report these
    properties and allow the catalog to display them nicely.

    """

    if isinstance(activity, list):
        return [process_used_for(act) for act in activity]
    if activity is None:
        return None

    thing = {"@type": "https://schema.org/Thing"}
    thing["name"] = activity.get("title", "")

    if url := activity.get("url", False):
        thing["url"] = url

    if description := activity.get("description", False):
        if type(description) is list:
            # we allowed multi-paragraph entries across columns
            # which we now join using newlines to avoid having
            # to add paragraphs in the catalog
            description = "\n\n".join(description)
        thing["description"] = description

    return thing


def process_file(f):
    """Convert file information to catalog schema

    This gets item values (or @values, depending how they were defined
    in tabby expansion context), and does type conversion (bytesize to
    int). Returns a dictionary with catalog keys that can be read from
    tabby (does not contain type and dataset id/version).

    """

    d = {
        "path": f.get("path", {}).get("@value"),
        "contentbytesize": f.get("contentbytesize", {}).get("@value"),
        "url": f.get("url"),
    }

    if f.get("path") is None and f.get("name") is not None:
        # scoped context definition doesn't work for me as intended,
        # no idea why -- this would cover all bases
        d["path"] = f.get("name", {}).get("@value")

    if d.get("contentbytesize", False):
        # type conversion
        d["contentbytesize"] = int(d["contentbytesize"])

    return {k: v for k, v in d.items() if v is not None}


def process_homepage(homepage):
    """Return homepage as a dict or list of dict

    Returned dict will contain schema.org @type (URL) for usage in
    linked data scenarios.

    """
    if homepage is None:
        return None
    elif isinstance(homepage, list):
        return [process_homepage(hp) for hp in homepage]
    else:
        return {"@type": "https://schema.org/URL", "@value": homepage}


cat_context = {
    "schema": "https://schema.org/",
    "bibo": "https://purl.org/ontology/bibo/",
    "dcterms": "https://purl.org/dc/terms/",
    "nfo": "https://www.semanticdesktop.org/ontologies/2007/03/22/nfo/#",
    "obo": "https://purl.obolibrary.org/obo/",
    "openminds": "https://openminds.ebrains.eu/controlledTerms/",
    "name": "schema:name",
    "title": "schema:title",
    "description": "schema:description",
    "doi": "bibo:doi",
    "version": "schema:version",
    "license": "schema:license",
    "description": "schema:description",
    "authors": "schema:author",
    "orcid": "obo:IAO_0000708",
    "email": "schema:email",
    "keywords": "schema:keywords",
    "funding": {
        "@id": "schema:funding",
        "@context": {
            "funder": "schema:funder",
            "identifier": "schema:identifier",
        },
    },
    "publications": {
        "@id": "schema:citation",
        "@context": {
            "doi": "schema:identifier",
            "datePublished": "schema:datePublished",
            "citation": "schema:citation",
        },
    },
    "fileList": {
        "@id": "dcterms:hasPart",
        "@context": {
            "contentbytesize": "nfo:fileSize",
            "md5sum": "obo:NCIT_C171276",
            "path": "schema:name",
            "url": "schema:contentUrl",
        },
    },
    "address": "schema:PostalAddress",
    "sfbHomepage": "schema:mainEntityOfPage",
    "sfbDataController": "https://w3id.org/dpv#hasDataController",
    "sfbUsedFor": {
        "@id": "http://www.w3.org/ns/prov#hadUsage",
        "@context": {
            "url": "schema:url",
        },
    },
    "sfbProject": "schema:ResearchProject",
    "sfbSampleOrganism": "openminds:Species",
    "sfbSamplePart": "openminds:UBERONParcellation",
}

parser = ArgumentParser()
parser.add_argument("tabby_path", type=Path, help="Path to the tabby-dataset file")
parser.add_argument("-c", "--catalog", type=Path, help="Catalog to add to")
parser.add_argument("--set-as-super", action="store_true")
parser.add_argument("--remove-first", action="store_true")
parser.add_argument("--encoding", help="encoding to use when loading tabby")
args = parser.parse_args()

if args.encoding:
    record = load_tabby(
        args.tabby_path,
        cpaths=[Path(__file__).parent / "conventions"],
        encoding=args.encoding,
    )
else:
    record = load_tabby(
        args.tabby_path,
        cpaths=[Path(__file__).parent / "conventions"],
    )

expanded = jsonld.expand(record)
compacted = jsonld.compact(record, ctx=cat_context)

# Use catalog schema_utils to get base structure of metadata item
meta_item = get_metadata_item(
    item_type='dataset',
    dataset_id=mint_dataset_id(compacted.get("name"), compacted.get("sfbProject")),
    dataset_version=compacted.get("version"),
    source_name="tabby",
    source_version="0.1.0",
)
# note: this becomes catalog page title, so title fits better
meta_item["name"] = compacted.get("title")
meta_item["license"] = process_license(compacted.get("license"))
meta_item["description"] = compacted.get("description")
meta_item["doi"] = compacted.get("doi")
meta_item["authors"] = process_authors(compacted.get("authors"))
meta_item["keywords"] = process_keywords(compacted.get("keywords"))
meta_item["funding"] = process_funding(compacted.get("funding"))
meta_item["publications"] = process_publications(compacted.get("publications"))
meta_item["access_request_contact"] = process_arc(compacted.get("sfbDataController"))

# top display (displayed as properties)
# max items: 5
# note: long-ish text spills out on half-screen view
# currently not using

# additional display(s)
# note: to avoid having to do fancy expansion tricks in the catalog to show IRIs
# as functioning links, we are providing context explicitly here

sfb_additional_content = {
    "@context": {
        "homepage": "https://schema.org/mainEntityOfPage",
        "data controller": "https://w3id.org/dpv#hasDataController",
        "sample (organism)": "https://openminds.ebrains.eu/controlledTerms/Species",
        "sample (organism part)": "https://openminds.ebrains.eu/controlledTerms/UBERONParcellation",
        "CRC project": "https://schema.org/ResearchProject",
        "used for": "http://www.w3.org/ns/prov#hadUsage",
    },
    "sample (organism)": process_ols_term(
        compacted.get("sfbSampleOrganism"),
        repr_ncbitaxon,
    ),
    "sample (organism part)": process_ols_term(
        compacted.get("sfbSamplePart"),
        repr_uberon,
    ),
    "homepage": process_homepage(compacted.get("sfbHomepage")),
    "data controller": process_data_controller(compacted.get("sfbDataController")),
    "CRC project": compacted.get("sfbProject"),
    "used for": process_used_for(compacted.get("sfbUsedFor")),
}

# define an additional display tab for sfb content
meta_item["additional_display"] = [
    {
        "name": "SFB1451",
        "icon": "fa-solid fa-flask",
        "content": {k: v for k, v in sfb_additional_content.items() if v is not None},
    }
]

# Allow for self-description
is_tabby_self = False
if args.tabby_path.parent.match(".datalad/tabby/self") or (
    args.tabby_path.parent.match(".datalad/tabby")
    and args.tabby_path.name.startswith("self")
):
    # take id and version from datalad
    print("Path suggests it is a self-description of a dataset")
    ds = Dataset(get_dataset_root(args.tabby_path))
    meta_item["dataset_id"] = ds.id
    meta_item["dataset_version"] = ds.repo.get_hexsha()
    is_tabby_self = True

# Remove empty properties from the dataset metadata
meta_item = {k: v for k, v in meta_item.items() if v is not None}

# display what would be added to the catalog
pprint(meta_item)


# ---
# File handling
# File handling's different, because 1 file <-> 1 metadata object
# ---

# some metadata is constant for all files
# we copy dataset id & version from (dataset-level) meta_item
file_required_meta = get_metadata_item(
    item_type='file',
    dataset_id=meta_item.get("dataset_id"),
    dataset_version=meta_item.get("dataset_version"),
    source_name="tabby",
    source_version="0.1.0",
    exclude_keys=["path"],
)

# make a list of catalog-conforming dicts
cat_file_listing = []
for file_info in compacted.get("fileList", []):
    cat_file = file_required_meta | process_file(file_info)
    cat_file_listing.append(cat_file)

# -----
# Adding to a catalog
# -----


if args.catalog is not None:
    # If a catalog path was given, use that catalog
    catalog_dir = args.catalog
else:
    # Otherwise, use a testing one in cwd
    catalog_dir = Path("catalog")

# I need to know id and version to clear old / set super
dsid = meta_item["dataset_id"]
dsver = meta_item["dataset_version"]

print(dsid, dsver)

# why catalog is needed: https://github.com/datalad/datalad-catalog/issues/330
catalog_validate(catalog=catalog_dir, metadata=json.dumps(meta_item))

# If requested and present, remove existing entries for the dataset
# Ignored for tabby-self which presumably is not the only source of metadata
if args.remove_first and not is_tabby_self:
    try:
        catalog_remove(
            catalog=catalog_dir,
            dataset_id=dsid,
            dataset_version=dsver,
            reckless=True,
            on_failure="continue",
        )
    except IncompleteResultsError:
        pass

# Add dataset metadata to the catalog
catalog_add(
    catalog=catalog_dir,
    metadata=json.dumps(meta_item),
    config_file=catalog_dir / "config.json",
)

# Add file listing to the catalog
for cat_file in cat_file_listing:
    catalog_add(
        catalog=catalog_dir,
        metadata=json.dumps(cat_file),
        config_file=catalog_dir / "config.json",
    )

# If requested, set the catalog superdataset to the recently added one
if args.set_as_super:
    catalog_set(
        catalog=catalog_dir,
        property="home",
        dataset_id=dsid,
        dataset_version=dsver,
        reckless="overwrite",
    )

"""
Notes

- the "date modified" in the catalog comes from the source field
  should we maybe copy the tabby value? is it meant to be dataset or catalog modification page?
"""
