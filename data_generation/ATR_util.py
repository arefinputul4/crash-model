import re

import geocoder
import openpyxl
import fiona
from shapely.geometry import Point, shape, mapping
import pyproj
import csv
from time import sleep
import matplotlib.pyplot as pyplot
import rtree

PROJ = pyproj.Proj(init='epsg:3857')
MAP_FP = '../data/processed/maps'


def is_readable_ATR(fname):
    """
    Function to check if ATR is of type we want to read
    checking for 3 conditions

     1) of 'XXX' type (contains speed, volume, and classification data)
     2) of 24-HOURS type
     3) .XLSX file type 
    """

    # split file name so we can check relevant info
    meta_info = fname.split('_')
    file_type = meta_info[8].split('.')[1]

    # only look at files that are .xlsx, are over 24 hours, and are of type XXX
    if (meta_info[7] == 'XXX') and (meta_info[6] == '24-HOURS') and (file_type == 'XLSX'):
        return True
    else:
        return False


def clean_ATR_fname(fname):
    """
    Clean filename to prepare for geocoding
    EX:
    7362_NA_NA_147_TRAIN-ST_DORCHESTER_24-HOURS_XXX_03-19-2014.XLSX
    to
    147 TRAIN ST Boston, MA
    """

    atr_address = fname.split('_') # split address on underscore character
    atr_address = ' '.join(atr_address[3:5]) # combine elements that make up the address
    atr_address = re.sub('-', ' ', atr_address) # replace '-' with spaces
    atr_address += ' Boston, MA'
    return atr_address


def geocode_address(address):
    """
    Use google's API to look up the address
    Due to rate limiting, try a few times with an increasing
    wait if no address is found

    Args:
        address
    Returns:
        address, latitude, longitude
    """
    g = geocoder.google(address)
    attempts = 0
    while g.address is None and attempts < 3:
        attempts += 1
        sleep(attempts ** 2)
        g = geocoder.google(address)
    return g.address, g.lat, g.lng


def plot_hourly_rates(files, outfile):
    """
    Function that reads ATRs and generates a sparkline plot
    of percentages of traffic over time
    
    Args:
        files - list of filenames to process
        outfile - where to write the resulting plot
    """

    all_counts = []
    for f in files:
        wb = openpyxl.load_workbook(f, data_only=True)
        sheet_names = wb.get_sheet_names()
        if 'Classification-Combined' in sheet_names:
            sheet = wb.get_sheet_by_name('Classification-Combined')
            # Right now the cell locations are hardcoded,
            # but if we expand to cover different formats, will need to change
            counts = []
            for row_index in range(9, 33):
                cell = "{}{}".format('O', row_index)
                val = sheet[cell].value
                counts.append(float(val))
            total = sheet['O34'].value
            for i in range(len(counts)):
                counts[i] = counts[i]/total
            all_counts.append(counts)

    bins = range(0, 24)
    for val in all_counts:
        pyplot.plot(bins, val)
    pyplot.legend(loc='upper right')
    pyplot.savefig(outfile)


def read_ATR(fname):
    """
    Function to read ATR data
    data to collect:
    mean speed, volume, motos (# of motorcycles), light(# of cars/trucks), 
    and heavy(# of heavy duty vehicles)
    """

    # data_only=True so as to not read formulas
    wb = openpyxl.load_workbook(fname, data_only=True)
    sheet_names = wb.get_sheet_names()

    # get total volume cell F106
    if 'Volume' in sheet_names:
        sheet = wb.get_sheet_by_name('Volume')
        vol = sheet['F106'].value
    else:
        vol = 0

    # get mean speed data
    if 'Speed Combined' in sheet_names:
        sheet = wb.get_sheet_by_name('Speed Combined')
        speed = sheet['E42'].value
    elif 'Speed-1' in sheet_names:
        sheet = wb.get_sheet_by_name('Speed-1')
        speed = sheet['E42'].value
    else:
        speed = 0

    # get classification data
    if 'Classification-Combined' in sheet_names:
        sheet = wb.get_sheet_by_name('Classification-Combined')
        motos = sheet['D38'].value
        light = sheet['D39'].value
        heavy = sheet['D40'].value
    elif 'Classification-1' in sheet_names:
        sheet = wb.get_sheet_by_name('Classification-1')
        motos = sheet['D38'].value
        light = sheet['D39'].value
        heavy = sheet['D40'].value
    else:
        motos = 0
        light = 0
        heavy = 0

    return vol, speed, motos, light, heavy


def read_shp(fp):
    """ Read shp, output tuple geometry + property """
    out = [(shape(line['geometry']), line['properties'])
           for line in fiona.open(fp)]
    return(out)


def read_record(record, x, y, orig=None, new=PROJ):
    """
    Reads record, outputs dictionary with point and properties
    Specify orig if reprojecting
    """
    if (orig is not None):
        x, y = pyproj.transform(orig, new, x, y)
    r_dict = {
        'point': Point(float(x), float(y)),
        'properties': record
    }
    return(r_dict)


def csv_to_projected_records(filename, x='X', y='Y'):
    """
    Reads a csv file in and creates a list of records,
    projecting x and y coordinates to projection 4326

    Args:
        filename (csv file)
        optional:
            x coordinate name (defaults to 'X')
            y coordinate name (defaults to 'Y')
    """
    records = []
    with open(filename) as f:
        csv_reader = csv.DictReader(f)
        for r in csv_reader:
            # Can possibly have 0 / blank coordinates
            if r[x] != '':
                records.append(
                    read_record(r, r[x], r[y],
                                orig=pyproj.Proj(init='epsg:4326'))
                )
    return records


def read_segments():
    # Read in segments
    inter = read_shp(MAP_FP + '/inters_segments.shp')
    non_inter = read_shp(MAP_FP + '/non_inters_segments.shp')
    print "Read in {} intersection, {} non-intersection segments".format(
        len(inter), len(non_inter))

    # Combine inter + non_inter
    combined_seg = inter + non_inter

    # Create spatial index for quick lookup
    segments_index = rtree.index.Index()
    for idx, element in enumerate(combined_seg):
        segments_index.insert(idx, element[0].bounds)
    return combined_seg, segments_index


def find_nearest(records, segments, segments_index, tolerance):
    """ Finds nearest segment to records
    tolerance : max units distance from record point to consider
    """

    print "Using tolerance {}".format(tolerance)

    for record in records:
        record_point = record['point']
        record_buffer_bounds = record_point.buffer(tolerance).bounds
        nearby_segments = segments_index.intersection(record_buffer_bounds)
        segment_id_with_distance = [
            # Get db index and distance to point
            (
                segments[segment_id][1]['id'],
                segments[segment_id][0].distance(record_point)
            )
            for segment_id in nearby_segments
        ]
        # Find nearest segment
        if len(segment_id_with_distance):
            nearest = min(segment_id_with_distance, key=lambda tup: tup[1])
            db_segment_id = nearest[0]
            # Add db_segment_id to record
            record['properties']['near_id'] = db_segment_id
        # If no segment matched, populate key = ''
        else:
            record['properties']['near_id'] = ''


def write_shp(schema, fp, data, shape_key, prop_key):
    """ Write Shapefile
    schema : schema dictionary
    shape_key : column name or tuple index of Shapely shape
    prop_key : column name or tuple index of properties
    """
    with fiona.open(fp, 'w', 'ESRI Shapefile', schema) as c:
        for i in data:
            c.write({
                'geometry': mapping(i[shape_key]),
                'properties': i[prop_key],
            })

