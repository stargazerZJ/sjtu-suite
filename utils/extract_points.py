'''
Extracts all points (location and seconds only) from a JSON file into a flat list.
This file is used to extract points from a REAL running data.
'''
import json
import argparse

def extract_points_from_json(input_file_path, output_file_path):
    """
    Extracts only the points (location and seconds) from a JSON file 
    and saves them as a flat list to a new JSON file. 
    All other original JSON structure is discarded.
    The 'locatetime' field within points is omitted.
    """
    try:
        with open(input_file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"Error: Input file not found at {input_file_path}")
        return
    except json.JSONDecodeError:
        print(f"Error: Could not decode JSON from {input_file_path}")
        return

    all_extracted_points = [] # This will be a flat list of points

    if not isinstance(data, list):
        print("Error: Expected a list at the root of the JSON data.")
        return
        
    for item in data:
        if isinstance(item, dict) and "tracks" in item and isinstance(item["tracks"], list):
            for track in item["tracks"]:
                if isinstance(track, dict) and "points" in track and isinstance(track["points"], list):
                    for point in track["points"]:
                        if isinstance(point, dict) and \
                           "location" in point and \
                           "seconds" in point:
                            all_extracted_points.append({
                                "location": point["location"],
                                "seconds": point["seconds"]
                            })
                        # Points not matching the structure or missing keys are skipped

    try:
        with open(output_file_path, 'w', encoding='utf-8') as f:
            json.dump(all_extracted_points, f, indent=2, ensure_ascii=False)
        print(f"Successfully extracted points to {output_file_path}")
    except IOError:
        print(f"Error: Could not write to output file at {output_file_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Extracts all points (location and seconds only) from a JSON file into a flat list. Discards all other original structure and 'locatetime' from points. Saves to a new JSON file."
    )
    parser.add_argument("input_file", help="Path to the input JSON file.")
    parser.add_argument("output_file", help="Path to the output JSON file.")
    args = parser.parse_args()

    extract_points_from_json(args.input_file, args.output_file)
