$fn = 40;
bottom();
module top(){
difference(){
translate([0,0,-20])plate(249.5,178.5,10,20);
translate([5,5,-23])plate(240,168.5,5,20);
    
    translate([90,8,-10])cube([151,46,40]);
    translate([55,68,-10])cube([175,100,30]);
    
    translate([50,62,-0.5])plate(194,112,8,4);
    translate([12,142,-5])neutrik();
    translate([12,110,-5])neutrik();
    translate([12,78,-5])neutrik();

    translate([12,18,-5])neutrik();
}

translate([0,56,-20])cube([249.5,5,19]);
translate([44,0,-20])cube([5,178,19]);
}
module neutrik(){
    hull(){
        translate([0,0,0])cylinder(d=2,h=2);
        translate([24,29,0])cylinder(d=2,h=2);
        translate([24,0,0])cylinder(d=2,h=2);
        translate([0,29,0])cylinder(d=2,h=2);
    }
    translate([12,14.5,2])cylinder(d=24,h=5);
    translate([2.5,26.5,2])cylinder(d=3.2,h=5);
    translate([2.5+19,26.5-24,2])cylinder(d=3.2,h=5);
}

//difference(){
//    bottom();
//    translate([125,-10,-10])cube([300,300,300]);
//}

module bottom(){
difference(){
hull(){
translate([0,0,15])plate(250,178,10,10);
translate([10,10,0])plate(230,160,10,15);
    
}
translate([30,25,-0.01])cylinder(d=6.5,h=1);
translate([30+190,25,-0.01])cylinder(d=6.5,h=1);
translate([30+190,130+25,-0.01])cylinder(d=6.5,h=1);
translate([30,130+25,-0.01])cylinder(d=6.5,h=1);

translate([13,13,3])plate(224,154,8,30);

translate([6,6,18])plate(238,135,9,10);

translate([30,25,-0.01])cylinder(d=3,h=10);
translate([30+190,25,-0.01])cylinder(d=3,h=10);
translate([30+190,130+25,-0.01])cylinder(d=3,h=10);
translate([30,130+25,-0.01])cylinder(d=3,h=10);
}

}

module plate(width,depth,corner,height){hull(){
    translate([corner, corner, 0])cylinder(r=corner,h=height);
    translate([width-corner, corner, 0])cylinder(r=corner,h=height);
    translate([width-corner, depth-corner, 0])cylinder(r=corner,h=height);
    translate([corner, depth-corner, 0])cylinder(r=corner,h=height);
}}


